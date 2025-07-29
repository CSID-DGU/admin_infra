from flask import Flask, request, jsonify
from kubernetes import client, config as k8s_config
import pymysql
import os

from dotenv import load_dotenv
load_dotenv()

app = Flask(__name__)

# DB 연결 설정 (수정필요)
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "password")
DB_NAME = os.getenv("DB_NAME", "web_admin")
DB_PORT = int(os.getenv("DB_PORT", 3306))


@app.route("/health", methods=["GET"])
def health():
    return "OK", 200


@app.route("/config", methods=["POST"])
def config():
    data = request.get_json(force=True)
    username = data.get("username")
    if not username:
        return jsonify({"error": "username is required"}), 400

    # Spring WAS로 사용자 승인정보 요청
    try:
        was_url = f"http://<WAS_URL>:<PORT>/api/acceptinfo/{username}"  # TODO: 실제 주소로 변경
        was_response = requests.get(was_url, timeout=3)
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
    cpu_limit = "1000m"
    memory_limit = "1024Mi"
    num_gpu = 0
    for node in gpu_nodes:
        if node["node_name"] == best_node:
            cpu_limit = node.get("cpu_limit", "1000m")
            memory_limit = node.get("memory_limit", "1024Mi")
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
            "mountPath": "/home/share",
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

    return jsonify({
        "config": {
            "backend": "kubernetes",
            "kubernetes": {
                "pod": {
                    "metadata": {
                        "namespace": "containerssh",
                        "labels": {
                            "app": "containerssh-guest",
                            "managed-by": "containerssh"
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
                                        "cpu": "1000m",
                                        "memory": "1024Mi"
                                    },
                                    "limits": {
                                        "cpu": "cpu_limit",
                                        "memory": "memory_limit"
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



def update_volume_size_in_db(username, new_size):
    conn = pymysql.connect(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASSWORD,
        db=DB_NAME,
        port=DB_PORT,
        charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor
    )
    try:
        with conn.cursor() as cursor:
            sql = """
            UPDATE approval
            SET volume_size = %s
            WHERE username = %s
            """
            # "Gi" -> int 변환 후 저장
            cursor.execute(sql, (int(new_size.replace("Gi", "")), username))

        conn.commit()
    finally:
        conn.close()

@app.route("/pvc", methods=["POST"])
def create_or_resize_pvc():
    data = request.get_json(force=True)
    username = data.get("username")
    storage_raw = data.get("storage")

    if not username or not storage_raw:
        return jsonify({"error": "username and storage are required"}), 400

    storage = f"{storage_raw}Gi"
    pvc_name = f"pvc-{username}-share"
    pv_name = f"pv-{username}-share"
    namespace = "containerssh"

    try:
        try:
            k8s_config.load_incluster_config()
        except:
            k8s_config.load_kube_config()

        core_v1 = client.CoreV1Api()

        try:
            existing_pvc = core_v1.read_namespaced_persistent_volume_claim(pvc_name, namespace)
            # 존재하면 resize
            patch_body = {
                "spec": {
                    "resources": {
                        "requests": {
                            "storage": storage
                        }
                    }
                }
            }
            core_v1.patch_namespaced_persistent_volume_claim(pvc_name, namespace, patch_body)
            update_volume_size_in_db(username, storage)
            return jsonify({"status": "resized", "message": f"{pvc_name} resized to {storage}"})

        except client.exceptions.ApiException as e:
            if e.status != 404:
                return jsonify({"error": f"Kubernetes API error: {e.body}"}), 500

        # 존재하지 않으면 새로 만들기
        pv_body = client.V1PersistentVolume(
            metadata=client.V1ObjectMeta(name=pv_name),
            spec=client.V1PersistentVolumeSpec(
                capacity={"storage": storage},
                access_modes=["ReadWriteMany"],
                storage_class_name="nfs-nas-v3-expandable",
                persistent_volume_reclaim_policy="Retain",
                nfs=client.V1NFSVolumeSource(
                    server="100.100.100.120",
                    path=f"/volume1/share/user-share/{username}"
                ),
                mount_options=["vers=3"]
            )
        )
        pvc_body = client.V1PersistentVolumeClaim(
            metadata=client.V1ObjectMeta(name=pvc_name),
            spec=client.V1PersistentVolumeClaimSpec(
                access_modes=["ReadWriteMany"],
                resources=client.V1ResourceRequirements(
                    requests={"storage": storage}
                ),
                storage_class_name="nfs-nas-v3-expandable",
                volume_name=pv_name
            )
        )
        core_v1.create_persistent_volume(body=pv_body)
        core_v1.create_namespaced_persistent_volume_claim(namespace, pvc_body)
        update_volume_size_in_db(username, storage)

        return jsonify({"status": "created", "message": f"{pvc_name} created with {storage}"})

    except client.exceptions.ApiException as e:
        return jsonify({"error": f"Kubernetes API error: {e.body}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@app.route("/resize-pvc", methods=["POST"])
def resize_pvc():
    data = request.get_json(force=True)
    username = data.get("username")
    storage_raw = data.get("storage")

    if not username or not storage_raw:
        return jsonify({"error": "username and storage are required"}), 400

    storage = f"{storage_raw}Gi"
    pvc_name = f"pvc-{username}-share"
    pv_name = f"pv-{username}-share"
    namespace = "containerssh"

    try:
        try:
            k8s_config.load_incluster_config()
        except:
            k8s_config.load_kube_config()

        core_v1 = client.CoreV1Api()

        # 기존 PVC, PV 삭제
        try:
            core_v1.delete_namespaced_persistent_volume_claim(pvc_name, namespace)
        except client.exceptions.ApiException as e:
            if e.status != 404:
                return jsonify({"error": f"Failed to delete PVC: {e.body}"}), 500
        try:
            core_v1.delete_persistent_volume(pv_name)
        except client.exceptions.ApiException as e:
            if e.status != 404:
                return jsonify({"error": f"Failed to delete PV: {e.body}"}), 500

        import time
        time.sleep(3)  # 삭제 반영 대기

        # 새 PV/PVC 생성
        pv_body = client.V1PersistentVolume(
            metadata=client.V1ObjectMeta(name=pv_name),
            spec=client.V1PersistentVolumeSpec(
                capacity={"storage": storage},
                access_modes=["ReadWriteMany"],
                storage_class_name="nfs-nas-v3-expandable",
                persistent_volume_reclaim_policy="Retain",
                nfs=client.V1NFSVolumeSource(
                    server="100.100.100.120",
                    path=f"/volume1/share/user-share/{username}"
                ),
                mount_options=["vers=3"]
            )
        )
        pvc_body = client.V1PersistentVolumeClaim(
            metadata=client.V1ObjectMeta(name=pvc_name),
            spec=client.V1PersistentVolumeClaimSpec(
                access_modes=["ReadWriteMany"],
                resources=client.V1ResourceRequirements(
                    requests={"storage": storage}
                ),
                storage_class_name="nfs-nas-v3-expandable",
                volume_name=pv_name
            )
        )

        core_v1.create_persistent_volume(body=pv_body)
        core_v1.create_namespaced_persistent_volume_claim(namespace, pvc_body)

        # DB에 반영
        update_volume_size_in_db(username, storage)

        return jsonify({"status": "resized", "message": f"{pvc_name} resized by recreating with {storage}"}), 200

    except client.exceptions.ApiException as e:
        return jsonify({"error": f"Kubernetes API error: {e.body}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500



def select_best_node_from_prometheus(node_list):
    PROM_URL = "http://210.94.179.19:9750"
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
            response = requests.get(f"{PROM_URL}/api/v1/query", params={"query": query}, timeout=2)
            value = float(response.json()["data"]["result"][0]["value"][1])
        except:
            value = float("inf")

        if value < best_score:
            best_score = value
            best_node = node

    return best_node



if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)

