import subprocess
from kubernetes import client, config as k8s_config

def load_k8s():
    # k8s client 초기
    try:
        k8s_config.load_incluster_config()
    except:
        k8s_config.load_kube_config()

def pod_has_process(namespace, pod_name, username):
    import subprocess
    
    try:
        uid_cmd = [
            "kubectl", "exec", "-n", namespace, pod_name, "--",
            "id", "-u", username
        ]
        uid_result = subprocess.run(uid_cmd, capture_output=True, text=True, check=True)
        user_uid = uid_result.stdout.strip()

        # 프로세스 목록 조회
        ps_cmd = [
            "kubectl", "exec", "-n", namespace, pod_name, "--",
            "ps", "-eo", "pid,uid,cmd", "--no-headers"
        ]
        result = subprocess.run(ps_cmd, capture_output=True, text=True, check=True)
        processes = result.stdout.strip().split("\n")

        system_cmds = [
            "ps", "bash", "sh", "sleep", "top", "kubectl", "tail", "cat"
        ]

        # 필터링
        user_procs = [
            proc for proc in processes
            if proc and proc.split()[1] == user_uid
            and not any(proc.split(maxsplit=2)[2].startswith(syscmd) for syscmd in system_cmds)
        ]

        return len(user_procs) > 0

    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Failed to check processes in Pod: {e}")
        return False


def delete_pod(pod_name, namespace):
    # Pod 삭제
    v1 = client.CoreV1Api()
    v1.delete_namespaced_pod(pod_name, namespace)
