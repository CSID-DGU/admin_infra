"""
v1.0: operation_log(log-mysql/operation_state_db)에 기록

main.py의 실제 계정/Pod 생성 로직은 변경되지 않으며,
그 로직 사이사이에 log_operation() 호출만 끼워 넣는 방식으로 사용해 로그를 기록
"""
from datetime import datetime
from enum import Enum

from flask import current_app as app

from utils import get_log_db_connection


class Action(str, Enum):
    """
    action 속성에 들어갈 값 전체 목록
    """

    CREATE_ACCOUNT = "CREATE_ACCOUNT"
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


class Phase(str, Enum):
    START = "START"
    SUCCESS = "SUCCESS"
    FAIL = "FAIL"
    RETRY = "RETRY"


def _lookup_start_time(conn, request_id, action, attempt):
    """
    같은 (request_id, action, attempt)의 가장 최근 START 행 created_at을 찾음
    duration_ms 계산에 사용
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT created_at FROM operation_log "
            "WHERE request_id=%s AND action=%s AND attempt=%s AND phase=%s "
            "ORDER BY id DESC LIMIT 1",
            (request_id, action, attempt, Phase.START.value),
        )
        row = cur.fetchone()
        return row[0] if row else None


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
    같은 (request_id, action, attempt)의 START 시각을 조회해서 자동으로 계산해 채움
    """
    action_value = action.value if isinstance(action, Action) else action
    phase_value = phase.value if isinstance(phase, Phase) else phase

    conn = None
    try:
        conn = get_log_db_connection()

        if duration_ms is None and phase_value in (Phase.SUCCESS.value, Phase.FAIL.value):
            start_at = _lookup_start_time(conn, request_id, action_value, attempt)
            if start_at is not None:
                duration_ms = int((datetime.now() - start_at).total_seconds() * 1000)

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
