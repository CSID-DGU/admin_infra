"""GPU 유실 점검 CronJob.

GPU 디바이스는 컨테이너 생성 시점에만 주입되고 그 이후 호스트에서 재생성되면
갱신되지 않는다 (#139/#140 참고). 이미 떠 있는 컨테이너를 무중단으로 복구할 방법이
없어서, 자동 재시작 대신 GPU가 사라진 걸 감지하면 관리자에게 Slack으로만 알리고
실제 재시작/재생성은 사람이 직접 판단해서 진행한다 (#146 — 자동 재시작은 사용자
동의 없이 진행 중인 작업을 강제로 끊을 수 있어 채택하지 않음).
"""
from main import app, load_k8s
from kubernetes import client
from kubernetes.stream import stream
import requests

NAMESPACE = "ailab-infra"
GPU_POD_LABEL_SELECTOR = "has-gpu=true"
EXEC_TIMEOUT_SEC = 15


def check_gpu_pods() -> None:
    load_k8s()
    v1 = client.CoreV1Api()

    try:
        pods = v1.list_namespaced_pod(NAMESPACE, label_selector=GPU_POD_LABEL_SELECTOR).items
    except Exception as e:
        app.logger.error(f"[GPU CHECK] GPU pod 목록 조회 실패: {e}")
        return

    app.logger.info(f"[GPU CHECK] 점검 대상 {len(pods)}개")

    for pod in pods:
        pod_name = pod.metadata.name
        if pod.status.phase != "Running":
            app.logger.debug(f"[GPU CHECK] Running 아님, 스킵: pod={pod_name} phase={pod.status.phase}")
            continue

        username = (pod.metadata.labels or {}).get("username", "unknown")
        node_name = pod.spec.node_name

        try:
            output = stream(
                v1.connect_get_namespaced_pod_exec,
                pod_name, NAMESPACE,
                container="shell",
                command=["sh", "-c", "nvidia-smi -L"],
                stderr=True, stdin=False, stdout=True, tty=False,
                _request_timeout=EXEC_TIMEOUT_SEC,
            )
        except Exception as e:
            # exec 자체가 실패하는 건 sshd/컨테이너 상태 문제일 수 있어 GPU 유실로
            # 단정하지 않는다 — 로그만 남기고 다음 pod으로 넘어간다.
            app.logger.warning(f"[GPU CHECK] exec 실패, GPU 상태 판단 불가: pod={pod_name} — {e}")
            continue

        if "GPU" in output:
            app.logger.debug(f"[GPU CHECK] 정상: pod={pod_name}")
            continue

        app.logger.error(f"[GPU CHECK] GPU 유실 감지: pod={pod_name} username={username} node={node_name}")
        _alert_gpu_lost(pod_name, username, node_name)


def _alert_gpu_lost(pod_name: str, username: str, node_name: str) -> None:
    webhook_url = app.config["INFRA_SLACK_WEBHOOK_URL"]
    if not webhook_url:
        app.logger.warning("[GPU CHECK] INFRA_SLACK_WEBHOOK_URL 미설정 — Slack 알림 스킵 (로그만 남김)")
        return

    message = (
        ":warning: *GPU 유실 감지*\n"
        f"▶ pod: {pod_name}\n"
        f"▶ 사용자: {username}\n"
        f"▶ 노드: {node_name}\n"
        "▶ nvidia-smi에서 GPU가 조회되지 않습니다. 자동 재시작은 하지 않습니다 "
        "(사용자 작업 중일 수 있음) — 확인 후 필요 시 사용자와 조율해서 pod을 재생성해주세요."
    )
    try:
        resp = requests.post(
            f"{app.config['ADMIN_BE_INTERNAL_URL']}/api/internal/slack/notify",
            json={"webhookUrl": webhook_url, "message": message},
            timeout=10,
        )
        resp.raise_for_status()
    except Exception as e:
        app.logger.error(f"[GPU CHECK] Slack 알림 전송 실패: pod={pod_name} — {e}")


if __name__ == "__main__":
    with app.app_context():
        check_gpu_pods()
