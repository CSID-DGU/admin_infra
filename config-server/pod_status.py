import os
import json
import redis
from datetime import datetime, timezone

REDIS_HOST = os.getenv("REDIS_HOST", "redis-bg-master.ailab-infra.svc.cluster.local")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)

STATUS_TTL_SEC = 3600  # 완료/실패 후에도 조회 가능하도록 1시간 유지, 이후 자동 만료


def set_pod_creation_status(username: str, stage: str, message: str = "") -> None:
    key = f"pod_status:{username}"
    data = {
        "stage": stage,
        "message": message,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        r.set(key, json.dumps(data), ex=STATUS_TTL_SEC)
    except Exception:
        pass  # 상태 조회는 부가 기능 — Redis 장애가 pod 생성 자체를 막으면 안 됨


def get_pod_creation_status(username: str):
    key = f"pod_status:{username}"
    raw = r.get(key)
    if raw is None:
        return None
    return json.loads(raw)
