from flask import Flask, request, jsonify
from kubernetes import client, config as k8s_config
import os
import pymysql

from dotenv import load_dotenv
load_dotenv()

from utils import get_existing_pod   # Pod 재사용 확인
from bg_redis import save_background_status
from utils import pod_has_process, delete_pod

app = Flask(__name__)

NAMESPACE = os.getenv("NAMESPACE", "default")


@app.route("/health", methods=["GET"])
def health():
    return "OK", 200


@app.route("/config", methods=["POST"])
def config():
    data = request.get_json(force=True)
    username = data.get("username")
    if not username:
        return jsonify({"error": "username is required"}), 400

    # 현재 실행 중인 Pod 확인 → 있으면 attach 모드로 반환
    existing_pod = get_existing_pod(NAMESPACE, username)
    if existing_pod:
        return jsonify({
            "config": {
                "backend": "kubernetes",
                "kubernetes": {
                    "pod": {
                        "attach": {
                            "podName": existing_pod,
                            "namespace": NAMESPACE,
                            "container": "shell"
                        }
                    }
                }
            },
            "environment": {
                "USER": {"value": username, "sensitive": False}
            },
            "metadata": {},
            "files": {}
        })

    # Mock 사용자 정보 (Spring WAS / Prometheus 없이 테스트용)
    user_info = {
        "username": username,
        "image": "dguailab/containerssh-guest:cuda12.2-cudnn8-dev-ubuntu22.04",
        "uid": 1000,
        "gid": 1000,
        "gpu_required": True,
        "gpu_nodes": [
            {"node_name": "FARM8", "num_gpu": 2}
        ]
    }

    best_node = user_info["gpu_nodes"][0]["node_name"]
    image = user_info["image"]
    uid = user_info["uid"]
    gid = user_info["gid"]
    gpu_required = user_info.get("gpu_required", False)
    gpu_nodes = user_info.get("gpu_nodes", [])
    num_gpu = gpu_nodes[0]["num_gpu"]

    # 기본 볼륨 (유저 PVC)
    volume_mounts = [{
        "name": "user-home",
        "mountPath": "/home/share",
        "readOnly": False
    }]
    volumes = [{
        "name": "user-home",
        "persistentVolumeClaim": {
            "claimName": f"pvc-{username}-share"
        }
    }]

    # GPU 장치 마운트 추가
    if gpu_required and num_gpu > 0:
        for i in range(num_gpu):
            volume_mounts.append({
                "name": f"nvidia{i}",
                "mountPath": f"/dev/nvidia{i}"
            })
            volumes.append({
                "name": f"nvidia{i}",
                "hostPath": {
                    "path": f"/dev/nvidia{i}",
                    "type": "CharDevice"
                }
            })
        for dev in ["nvidiactl", "nvidia-uvm", "nvidia-uvm-tools", "nvidia-modeset"]:
            volume_mounts.append({
                "name": dev,
                "mountPath": f"/dev/{dev}"
            })
            volumes.append({
                "name": dev,
                "hostPath": {
                    "path": f"/dev/{dev}",
                    "type": "CharDevice"
                }
            })

    # host-etc 마운트 추가 (/etc/passwd, group, shadow, sudoers, bash.bash_logout)
    host_etc_mounts = [
        {"name": "host-etc", "mountPath": "/etc/passwd", "subPath": "passwd", "readOnly": True},
        {"name": "host-etc", "mountPath": "/etc/group", "subPath": "group", "readOnly": True},
        {"name": "host-etc", "mountPath": "/etc/shadow", "subPath": "shadow", "readOnly": True},
        {"name": "host-etc", "mountPath": f"/etc/sudoers.d/{username}", "subPath": f"sudoers.d/{username}", "readOnly": True},
        {"name": "host-etc", "mountPath": "/etc/bash.bash_logout", "subPath": "bash.bash_logout", "readOnly": True}
    ]
    volume_mounts.extend(host_etc_mounts)

    volumes.append({
        "name": "host-etc",
        "hostPath": {
            "path": "/etc",
            "type": "Directory"
        }
    })

    # Pod spec 반환 (없으면 새로 생성)
    return jsonify({
        "config": {
            "backend": "kubernetes",
            "kubernetes": {
                "pod": {
                    "metadata": {
                        "namespace": NAMESPACE,
                        "labels": {
                            "app": "containerssh-guest",
                            "managed-by": "containerssh",
                            "user": username
                        }
                    },
                    "spec": {
                        "nodeName": best_node,
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": uid,
                            "fsGroup": gid
                        },
                        "containers": [
                            {
                                "name": "shell",
                                "image": image,
                                "command": ["/bin/bash"],
                                "stdin": True,
                                "tty": True,
                                "env": [
                                    {"name": "USER", "value": username},
                                    {"name": "HOME", "value": f"/home/{username}"},
                                    {"name": "SHELL", "value": "/bin/bash"}
                                ],
                                "resources": {
                                    "requests": {"cpu": "1000m", "memory": "1024Mi"},
                                    "limits": {"cpu": "1000m", "memory": "1024Mi"}
                                },
                                "volumeMounts": volume_mounts
                            }
                        ],
                        "volumes": volumes,
                        "restartPolicy": "Never"
                    }
                }
            }
        },
        "environment": {
            "USER": {"value": username, "sensitive": False}
        },
        "metadata": {},
        "files": {}
    })


@app.route("/report-background", methods=["POST"])
def report_background():
    data = request.get_json(force=True)
    username = data.get("username")
    pod_name = data.get("pod_name")
    has_background = data.get("has_background", False)

    if not username or not pod_name:
        return jsonify({"error": "username and pod_name are required"}), 400

    still_running = pod_has_process(pod_name, NAMESPACE, username)
    if not still_running:
        delete_pod(pod_name, NAMESPACE)
        delete_user_status(username)
        return jsonify({"status": "deleted", "username": username}), 200

    save_background_status(username, pod_name, True)
    return jsonify({"status": "background", "username": username, "has_background": True}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)

