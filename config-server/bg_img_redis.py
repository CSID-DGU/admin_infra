import os
import json
import redis
from datetime import datetime

REDIS_HOST = os.getenv("REDIS_HOST", "redis-bg-master.cssh.svc.cluster.local")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB, decode_responses=True)

# ----------------------------
# 백그라운드 프로세스 관리
# ----------------------------
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


# ----------------------------
# 이미지 메타데이터 관리
# ----------------------------

def save_image_metadata(username, status="success", size_mb=0.0, version=None, path=None):
    """커밋 or 로드 시점에서 Redis에 이미지 상태 저장"""
    key = f"img:{username}"
    data = {
        "status": status,
        "size_mb": size_mb,
        "version": version,
        "path": path,
        "last_update": datetime.utcnow().isoformat()
    }
    r.set(key, json.dumps(data))
    return data


def get_image_metadata(username):
    """특정 유저의 이미지 상태 조회"""
    key = f"img:{username}"
    if not r.exists(key):
        return None
    return json.loads(r.get(key))


def get_all_images():
    """모든 이미지 상태 조회"""
    keys = r.keys("img:*")
    result = {}
    for key in keys:
        value = json.loads(r.get(key))
        result[key.replace("img:", "")] = value
    return result


def delete_image_metadata(username):
    """유저 이미지 메타데이터 삭제"""
    r.delete(f"img:{username}")

