from flask import Flask, request, jsonify
from kubernetes import client, config as k8s_config
import pymysql
import os
import requests

from dotenv import load_dotenv
load_dotenv()

from bg_redis import save_background_status
from utils import get_existing_pod


app = Flask(__name__)

# ---- Global app configuration ----
app.config.from_mapping({
    # Namespace
    "NAMESPACE": os.getenv("NAMESPACE", "default"),

    # External endpoints & timeouts
    "PROM_URL": os.getenv("PROM_URL", "http://210.94.179.19:9750"),
    "WAS_URL_TEMPLATE": os.getenv("WAS_URL_TEMPLATE", "http://210.94.179.19:9796/api/acceptinfo/{username}"),
    "HTTP_TIMEOUT_SEC": float(os.getenv("HTTP_TIMEOUT_SEC", "3.0")),

    # Default resources
    "DEFAULT_CPU_REQUEST": "1000m",
    "DEFAULT_MEM_REQUEST": "1024Mi",
    "DEFAULT_CPU_LIMIT":  "1000m",
    "DEFAULT_MEM_LIMIT":  "1024Mi",

    # PVC / storage policy
    "STORAGE_CLASS_NAME": os.getenv("STORAGE_CLASS_NAME", "nfs-nas-v3-expandable"),
    "PVC_NAME_PATTERN": "pvc-{username}-share",
    "PVC_ACCESS_MODES": ["ReadWriteMany"],
    "PVC_SIZE_UNIT": "Gi",

    # Mounts & devices
    "HOST_ETC_SUBPATHS": [
        "passwd",
        "group",
        "shadow",
        "sudoers.d/{username}",
        "bash.bash_logout",
    ],
    "NVIDIA_AUX_DEVICES": [
        "nvidiactl", "nvidia-uvm", "nvidia-uvm-tools", "nvidia-modeset"
    ],
})

@app.route("/health", methods=["GET"])
def health():
    return "OK", 200


@app.route("/config", methods=["POST"])
def config():
    data = request.get_json(force=True)
    username = data.get("username")
    if not username:
        return jsonify({"error": "username is required"}), 400

    # 현재 실행 중인 Pod가 있는지 확인
    ns = app.config["NAMESPACE"]
    existing_pod = get_existing_pod(ns, username)
    if existing_pod:
        # Pod가 이미 있으면 attach
        return jsonify({
            "config": {
                "backend": "kubernetes",
                "kubernetes": {
                    "pod": {
                        "attach": {
                            "podName": existing_pod,
                            "namespace": ns,
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

    # Pod 없으면 새로 생성

    # Spring WAS로 사용자 승인정보 요청
    try:
        was_url = app.config["WAS_URL_TEMPLATE"].format(username=username)
        was_response = requests.get(was_url, timeout=app.config["HTTP_TIMEOUT_SEC"])
        was_response.raise_for_status()
        user_info = was_response.json()
    except Exception as e:
        return jsonify({"error": f"Failed to fetch user info from WAS: {str(e)}"}), 500

    try:
        node_list = [node["node_name"] for node in user_info["gpu_nodes"]]
        best_node = select_best_node_from_prometheus(node_list)
    except Exception as e:
        return jsonify({"error": f"Failed to select best node: {str(e)}"}), 500

    image = user_info["image"]
    uid = user_info["uid"]
    gid = user_info["gid"]
    gpu_required = user_info.get("gpu_required", False)
    gpu_nodes = user_info.get("gpu_nodes", [])

    # best_node의 CPU/Memory limit 추출
    cpu_limit = app.config["DEFAULT_CPU_LIMIT"]
    memory_limit = app.config["DEFAULT_MEM_LIMIT"]
    num_gpu = 0
    for node in gpu_nodes:
        if node["node_name"] == best_node:
            cpu_limit = node.get("cpu_limit", app.config["DEFAULT_CPU_LIMIT"])
            memory_limit = node.get("memory_limit", app.config["DEFAULT_MEM_LIMIT"])
            num_gpu = node.get("num_gpu", 0)
            break

    num_gpu = 0
    for node in gpu_nodes:
        if node["node_name"] == best_node:
            num_gpu = node.get("num_gpu", 0)
            break

    volume_mounts = [
        {
            "name": "user-home",
            "mountPath": f"/home/{username}",
            "readOnly": False
        }
    ]
    volumes = [
        {
            "name": "user-home",
            "persistentVolumeClaim": {
                "claimName": f"pvc-{username}-share"
            }
        }
    ]

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
            mount_name = dev.replace("-", "")
            volume_mounts.append({
                "name": mount_name,
                "mountPath": f"/dev/{dev}"
            })
            volumes.append({
                "name": mount_name,
                "hostPath": {
                    "path": f"/dev/{dev}",
                    "type": "CharDevice"
                }
            })

    # host-etc 마운트 추가
    host_etc_mounts = []
    for sub in app.config["HOST_ETC_SUBPATHS"]:
        sub_fmt = sub.format(username=username)
        # decide mount path
        if sub.startswith("sudoers.d/"):
            mount_path = f"/etc/sudoers.d/{username}"
        else:
            mount_path = f"/etc/{sub_fmt}"
        host_etc_mounts.append({
            "name": "host-etc",
            "mountPath": mount_path,
            "subPath": sub_fmt,
            "readOnly": True
        })
    volume_mounts.extend(host_etc_mounts)

    volumes.append({
        "name": "host-etc",
        "hostPath": {
            "path": "/etc",
            "type": "Directory"
        }
    })



    return jsonify({
        "config": {
            "backend": "kubernetes",
            "kubernetes": {
                "pod": {
                    "metadata": {
                        "namespace": ns,
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
                                    "requests": {
                                        "cpu": app.config["DEFAULT_CPU_REQUEST"],
                                        "memory": app.config["DEFAULT_MEM_REQUEST"]
                                    },
                                    "limits": {
                                        "cpu": cpu_limit,
                                        "memory": memory_limit
                                    }
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
            "USER": {
                "value": username,
                "sensitive": False
            }
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

    ns = app.config["NAMESPACE"]
    # 실제 프로세스 확인
    still_running = pod_has_process(pod_name, ns, username)

    if not still_running:
        # 즉시 Pod 삭제
        delete_pod(pod_name, ns)
        delete_user_status(username)  # Redis 정리
        return jsonify({"status": "deleted", "username": username}), 200

    # 백그라운드 있으면 Redis에 저장
    save_background_status(username, pod_name, True)
    return jsonify({
        "status": "background",
        "username": username,
        "has_background": True
    }), 200


@app.route("/pvc", methods=["POST"])
def create_or_resize_pvc():
    data = request.get_json(force=True)
    username = data.get("username")
    storage_raw = data.get("storage")

    if not username or not storage_raw:
        return jsonify({"error": "username and storage are required"}), 400

    storage = f"{storage_raw}{app.config['PVC_SIZE_UNIT']}"
    pvc_name = app.config["PVC_NAME_PATTERN"].format(username=username)
    namespace = app.config["NAMESPACE"]

    try:
        try:
            k8s_config.load_incluster_config()
        except:
            k8s_config.load_kube_config()

        core_v1 = client.CoreV1Api()

        # PVC 존재 여부 확인
        try:
            _ = core_v1.read_namespaced_persistent_volume_claim(pvc_name, namespace)
            # 존재 → resize
            patch_body = {
                "spec": {
                    "resources": {
                        "requests": {
                            "storage": storage
                        }
                    }
                }
            }
            core_v1.patch_namespaced_persistent_volume_claim(
                name=pvc_name,
                namespace=namespace,
                body=patch_body
            )
            return jsonify({"status": "resized", "message": f"{pvc_name} resized to {storage}"})
        except client.exceptions.ApiException as e:
            if e.status != 404:
                return jsonify({"error": f"Kubernetes API error: {e.body}"}), 500

        # PVC 없으면 새로 생성
        pvc_body = client.V1PersistentVolumeClaim(
            metadata=client.V1ObjectMeta(
                name=pvc_name,
                annotations={"nfs.io/username": username}  # optional
            ),
            spec=client.V1PersistentVolumeClaimSpec(
                access_modes=app.config["PVC_ACCESS_MODES"],
                resources=client.V1ResourceRequirements(
                    requests={"storage": storage}
                ),
                storage_class_name=app.config["STORAGE_CLASS_NAME"]
            )
        )
        core_v1.create_namespaced_persistent_volume_claim(namespace, pvc_body)
        return jsonify({"status": "created", "message": f"{pvc_name} created with {storage}"})

    except Exception as e:
        return jsonify({"error": str(e)}), 500



@app.route("/resize-pvc", methods=["POST"])
def resize_pvc():
    data = request.get_json(force=True)
    username = data.get("username")
    storage_raw = data.get("storage")

    if not username or not storage_raw:
        return jsonify({"error": "username and storage are required"}), 400

    storage = f"{storage_raw}{app.config['PVC_SIZE_UNIT']}"
    pvc_name = app.config["PVC_NAME_PATTERN"].format(username=username)
    namespace = app.config["NAMESPACE"]

    try:
        try:
            k8s_config.load_incluster_config()
        except:
            k8s_config.load_kube_config()

        core_v1 = client.CoreV1Api()

        patch_body = {
            "spec": {
                "resources": {
                    "requests": {
                        "storage": storage
                    }
                }
            }
        }
        core_v1.patch_namespaced_persistent_volume_claim(
            name=pvc_name,
            namespace=namespace,
            body=patch_body
        )
        return jsonify({"status": "resized", "message": f"{pvc_name} resized to {storage}"})

    except client.exceptions.ApiException as e:
        return jsonify({"error": f"Kubernetes API error: {e.body}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500




def select_best_node_from_prometheus(node_list):
    prom_url = app.config["PROM_URL"]
    timeout = app.config["HTTP_TIMEOUT_SEC"]
    best_node = None
    best_score = float("inf")

    for node in node_list:
        query = f"""
        (
          (sum(k8s_namespace_pod_count_total{{hostname="{node}"}}) or vector(0)) +
          (count(gpu_process_memory_used_bytes{{hostname="{node}"}}) or vector(0))
        ) / (count by (gpu_uuid) (gpu_temperature_celsius{{hostname="{node}"}}) > 0 or vector(1))
        """
        try:
            response = requests.get(f"{prom_url}/api/v1/query", params={"query": query}, timeout=timeout)
            value = float(response.json()["data"]["result"][0]["value"][1])
        except:
            value = float("inf")

        if value < best_score:
            best_score = value
            best_node = node

    return best_node



if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
