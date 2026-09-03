from main import app, _get_farm_node_info, _remove_krb5_from_farm, _farm_ssh
from utils import get_db_connection


def reconcile_krb5_cleanup_pending() -> None:
    """krb5_cleanup_pending 테이블의 레코드를 순회하며 재정리를 시도한다."""
    conn = get_db_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT username, node_name FROM krb5_cleanup_pending")
        rows = cur.fetchall()
    conn.close()

    for username, node_name in rows:
        try:
            _remove_krb5_from_farm(username, node_name)
            conn = get_db_connection()
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM krb5_cleanup_pending WHERE username=%s AND node_name=%s",
                    (username, node_name),
                )
            conn.commit()
            conn.close()
            app.logger.info(f"[KRB5 RECONCILE] pending 정리 성공: {username} ← {node_name}")
        except Exception as e:
            app.logger.warning(f"[KRB5 RECONCILE] pending 정리 재시도 실패(다음 주기에 재시도): {username} ← {node_name} — {e}")


def _get_expected_krb5_usernames_for_node(node_name: str) -> set:
    """지금 이 노드에 떠 있어야 하는(=NodePort가 살아있는) username 집합."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT username FROM nodeport_allocations WHERE node_name=%s",
                (node_name,),
            )
            return {row[0] for row in cur.fetchall()}
    finally:
        conn.close()


def reconcile_krb5_orphans() -> None:
    """각 farm 노드의 keytab 목록과 '지금 이 노드에 떠 있어야 하는 username'(nodeport_allocations
    기준) 목록을 대조해 delete_pod/delete_user 흐름을 아예 타지 않은 고아(수동 조작, 코드 버그 등)를
    찾아낸다.

    주의 — 지금은 자동 삭제하지 않고 후보만 로그로 남긴다. 레거시(마이그레이션 전) Docker
    컨테이너 유저는 애초에 nodeport_allocations에 등록될 방법이 없어서, 이 대조만으로는
    "아직 신시스템으로 안 옮긴, 지금도 살아서 쓰이는 레거시 계정"과 "진짜 고아"를 구분할 수
    없다. 자동 삭제로 뒀다가 farm6의 레거시 계정 여러 개(2026-09-03 dry-run으로 확인, dm20020204/
    donghyun2/jy/ohchanju3)가 한꺼번에 지워질 뻔한 적이 있다. 레거시 계정을 구분할 방법(예:
    별도 allowlist/테이블)이 생기기 전까진 사람이 로그를 보고 직접 정리하는 게 안전하다."""
    for node in app.config["FARM_NODES"]:
        try:
            node_info = _get_farm_node_info(node["name"])
            result = _farm_ssh(node_info["host"], node_info["port"], "list")
            deployed_usernames = {u for u in result.splitlines() if u}
        except Exception as e:
            app.logger.warning(f"[KRB5 RECONCILE] {node['name']} keytab 목록 조회 실패: {e}")
            continue

        expected_usernames = _get_expected_krb5_usernames_for_node(node["name"])
        orphans = deployed_usernames - expected_usernames

        for username in orphans:
            app.logger.warning(
                f"[KRB5 RECONCILE] 고아 후보(자동 삭제 안 함, 확인 필요): {username} @ {node['name']} "
                "— nodeport_allocations엔 없지만 레거시 계정일 수 있음, 직접 확인 후 정리할 것"
            )


if __name__ == "__main__":
    with app.app_context():
        reconcile_krb5_cleanup_pending()
        reconcile_krb5_orphans()
