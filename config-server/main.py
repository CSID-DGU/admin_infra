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
    username = data.get("username", "default")
    server_number = data.get("server_number", "0")

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
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 1000,
                            "fsGroup": 1000
                        },
                        "containers": [
                            {
                                "name": "shell",
                                "image": "containerssh-guest:ubuntu22.04",
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
                                        "cpu": "100m",
                                        "memory": "128Mi"
                                    },
                                    "limits": {
                                        "cpu": "500m",
                                        "memory": "512Mi"
                                    }
                                },
                                "volumeMounts": [
                                    {
                                        "name": "user-home",
                                        "mountPath": "/home/share",
                                        "readOnly": False
                                    }
                                ]
                            }
                        ],
                        "volumes": [
                            {
                                "name": "user-home",
                                "persistentVolumeClaim": {
                                    "claimName": f"pvc-{username}-share"
                                }
                            }
                        ],
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
    new_size = f"{storage_raw}Gi"

    if not username or not new_size:
        return jsonify({"error": "username and storage are required"}), 400

    pvc_name = f"pvc-{username}-share"
    namespace = "containerssh"

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
                        "storage": new_size
                    }
                }
            }
        }

        core_v1.patch_namespaced_persistent_volume_claim(
            name=pvc_name,
            namespace=namespace,
            body=patch_body
        )

        # DB 업데이트
        update_volume_size_in_db(username, new_size)

        return jsonify({"status": "success", "message": f"{pvc_name} resized to {new_size}"}), 200

    except client.exceptions.ApiException as e:
        return jsonify({"error": f"Kubernetes API error: {e.body}"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)

