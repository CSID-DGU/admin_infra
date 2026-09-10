"""
v1.0: operation_log(log-mysql/operation_state_db)에 기록

main.py의 실제 계정/Pod 생성 로직은 변경되지 않으며,
그 로직 사이사이에 log_operation() 호출만 끼워 넣는 방식으로 사용해 로그를 기록
"""
from enum import Enum

from flask import current_app as app

from utils import get_log_db_connection


class Action(str, Enum):
    """
    action 속성에 들어갈 값 전체 목록
    """

    CREATE_ACCOUNT = "CREATE_ACCOUNT"
    # 계정 생성 API(/accounts/users) 안에서 계정 다음에 이어지는 두 단계. 계정과 action을
    # 나눠야 단계별 소요시간이 따로 잡히고, 어느 단계에서 실패했는지가 action만으로 드러난다.
    CREATE_HOME = "CREATE_HOME"
    CREATE_KRB5_PRINCIPAL = "CREATE_KRB5_PRINCIPAL"
    FETCH_USER_CONFIG = "FETCH_USER_CONFIG"  # WAS에서 사용자 설정 조회
    SELECT_NODE = "SELECT_NODE"              # Prometheus 기반 노드 선택
    ALLOCATE_NODEPORT = "ALLOCATE_NODEPORT"
    CREATE_POD_K8S = "CREATE_POD_K8S"
    WAIT_READY = "WAIT_READY"
    DEPLOY_KRB5 = "DEPLOY_KRB5"
    CREATE_SERVICE = "CREATE_SERVICE"
    DELETE_SERVICE = "DELETE_SERVICE"
    RELEASE_NODEPORT = "RELEASE_NODEPORT"
    DELETE_POD_K8S = "DELETE_POD_K8S"
    # 회수 경로. Pod/Service/NodePort 제거만 기록하면 계정과 인증 정보가 실제로 지워졌는지를
    # 이력에서 확인할 수 없다 — 회수 완료로 기록됐는데 접근 경로가 남는 경우를 판별하려면
    # 이 세 단계가 함께 남아야 한다.
    DELETE_ACCOUNT = "DELETE_ACCOUNT"
    DELETE_HOME = "DELETE_HOME"
    REMOVE_KRB5 = "REMOVE_KRB5"


class Phase(str, Enum):
    START = "START"
    SUCCESS = "SUCCESS"
    FAIL = "FAIL"
    RETRY = "RETRY"


def _lookup_elapsed_ms(conn, request_id, action, attempt):
    """
    같은 (request_id, action, attempt)의 가장 최근 START 행으로부터 지금까지 걸린 시간(ms)
    duration_ms 계산에 사용

    START 행의 created_at은 MySQL 서버 시계로 찍힌다. 끝 시각을 파이썬 쪽 datetime.now()로
    잡으면 config-server Pod와 MySQL Pod의 시계나 시간대 설정이 다를 때 값이 통째로
    어긋나므로(TZ가 한쪽에만 설정되면 9시간), 양쪽 끝을 모두 DB 시계로 잰다.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT TIMESTAMPDIFF(MICROSECOND, created_at, NOW(3)) DIV 1000 "
            "FROM operation_log "
            "WHERE request_id=%s AND action=%s AND attempt=%s AND phase=%s "
            "ORDER BY id DESC LIMIT 1",
            (request_id, action, attempt, Phase.START.value),
        )
        row = cur.fetchone()
        return int(row[0]) if row and row[0] is not None else None


def log_operation(
    *,
    request_id,
    username,
    action,
    phase,
    pod_name=None,
    node_name=None,
    resource_type=None,
    attempt=1,
    duration_ms=None,
    error_code=None,
    error_detail=None,
):
    """
    operation_log에 한 줄 기록. 절대 예외를 밖으로 던지지 않음
    로깅 실패가 실제 계정/Pod 생성 흐름을 막으면 안 되므로 실패하면 app.logger에만 남김

    duration_ms를 안 넘기고 phase가 SUCCESS/FAIL이면,
    같은 (request_id, action, attempt)의 START로부터 걸린 시간을 DB 시계로 계산해 채움
    """
    action_value = action.value if isinstance(action, Action) else action
    phase_value = phase.value if isinstance(phase, Phase) else phase
    # admin_be는 신청 PK를 숫자로 보낸다. 컬럼이 VARCHAR라 숫자 그대로 비교하면 MySQL이
    # 컬럼 쪽을 숫자로 변환해 인덱스를 못 타고, 숫자로 시작하는 다른 키와도 같다고 판정할
    # 수 있으므로 기록·조회 모두 문자열로 맞춘다.
    request_id = str(request_id)

    conn = None
    try:
        conn = get_log_db_connection()

        if duration_ms is None and phase_value in (Phase.SUCCESS.value, Phase.FAIL.value):
            duration_ms = _lookup_elapsed_ms(conn, request_id, action_value, attempt)

        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO operation_log "
                "(request_id, username, pod_name, node_name, resource_type, "
                " action, phase, attempt, duration_ms, error_code, error_detail) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    request_id, username, pod_name, node_name, resource_type,
                    action_value, phase_value, attempt, duration_ms,
                    error_code, error_detail,
                ),
            )
        conn.commit()

    except Exception:
        app.logger.exception(
            f"[OPERATION LOG] insert failed request_id={request_id} "
            f"action={action_value} phase={phase_value}"
        )
    finally:
        if conn is not None:
            conn.close()
