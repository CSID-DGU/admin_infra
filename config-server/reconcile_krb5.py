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
    """각 farm 노드의 keytab 목록과 '지금 이 노드에 떠 있어야 하는 username' 목록을 대조해
    delete_pod/delete_user 흐름을 아예 타지 않은 고아(수동 조작, 코드 버그 등)까지 잡아낸다."""
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
            app.logger.warning(f"[KRB5 RECONCILE] 고아 keytab 발견: {username} @ {node['name']} — 정리")
            try:
                _remove_krb5_from_farm(username, node["name"])
            except Exception as e:
                app.logger.warning(f"[KRB5 RECONCILE] 고아 정리 실패(다음 주기에 재시도): {username} @ {node['name']} — {e}")


if __name__ == "__main__":
    with app.app_context():
        reconcile_krb5_cleanup_pending()
        reconcile_krb5_orphans()
