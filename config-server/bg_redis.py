import os
import json
import redis
from datetime import datetime

REDIS_HOST = os.getenv("REDIS_HOST", "redis-bg-master.cssh.svc.cluster.local")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)

def save_background_status(username, pod_name, has_background):
    # 백그라운드 상태 저장
    data = {
        "pod_name": pod_name,
        "has_background": has_background,
        "last_checked": datetime.utcnow().isoformat()
    }
    r.set(f"bg:{username}", json.dumps(data))

def get_all_background_users():
    # 백그라운드 상태 전체조회
    keys = r.keys("bg:*")
    result = {}
    for key in keys:
        value = json.loads(r.get(key))
        result[key.replace("bg:", "")] = value
    return result

def delete_user_status(username):
    # 해당 pod 상태정보 삭제
    r.delete(f"bg:{username}")

