from datetime import datetime, timedelta
from bg_img_redis import get_all_background_users, delete_user_status
from utils import pod_has_process, delete_pod, load_k8s

NAMESPACE = "cssh"
FORCE_DELETE_AFTER = timedelta(weeks=2)  # 2주 이상 유지 시 강제 삭제

def main():
    load_k8s()
    users = get_all_background_users()
    now = datetime.utcnow()

    for username, info in users.items():
        pod_name = info["pod_name"]
        has_bg = info["has_background"]
        last_checked = datetime.fromisoformat(info["last_checked"])

        if has_bg:
            still_running = pod_has_process(pod_name, NAMESPACE, username)
            if not still_running:
                print(f"[{username}] Pod will be deleted.")
                delete_pod(pod_name, NAMESPACE)
                delete_user_status(username)
            elif now - last_checked > FORCE_DELETE_AFTER:
                print(f"[{username}] Exceeded {FORCE_DELETE_AFTER}, force deleting.")
                delete_pod(pod_name, NAMESPACE)
                delete_user_status(username)
            else:
                print(f"[{username}] Background process detected, keeping pod.")
        else:
            delete_user_status(username)

if __name__ == "__main__":
    main()

