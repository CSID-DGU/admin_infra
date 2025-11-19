from flask import Flask, request, jsonify, Blueprint
import fcntl
import re
import time
from typing import List, Optional
from kubernetes import client, config as k8s_config
import pymysql
import os
import requests
import logging, sys

from flasgger import Swagger

from dotenv import load_dotenv
load_dotenv()

import logging, sys

from bg_img_redis import save_background_status, delete_user_status
from utils import get_existing_pod
from utils import (
    get_existing_pod, pod_has_process, delete_pod,
    LockedFile,
    ensure_etc_layout, ensure_sudoers_dir,
    read_passwd_lines, write_passwd_lines,
    read_group_lines, write_group_lines,
    read_shadow_lines, write_shadow_lines,
    parse_passwd_line, format_passwd_entry,
    parse_group_line, format_group_entry,
    parse_shadow_line, format_shadow_entry,
    create_directory_with_permissions,
    delete_directory_if_exists,
    get_group_members_home_volumes,
    select_best_node_from_prometheus,
    load_user_image,
    create_nodeport_services,
    delete_nodeport_services,
)

app = Flask(__name__)

# 로그 설정
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.DEBUG)
formatter = logging.Formatter("[%(asctime)s] %(levelname)s in %(module)s: %(message)s")
handler.setFormatter(formatter)
app.logger.addHandler(handler)
app.logger.setLevel(logging.DEBUG)

# ---- Global app configuration ----
BASE_ETC_DIR = "/kube_share"
app.config.from_mapping({
    # Namespace
    "NAMESPACE": "cssh",

    # External endpoints & timeouts
    "PROM_URL": "http://210.94.179.19:9750",
    "WAS_URL_TEMPLATE": "http://210.94.179.19:9796/api/requests/config/{username}",
    "HTTP_TIMEOUT_SEC": 3.0,

    # Default resources
    "DEFAULT_CPU_REQUEST": "1000m",
    "DEFAULT_MEM_REQUEST": "1024Mi",
    "DEFAULT_CPU_LIMIT":  "1000m",
    "DEFAULT_MEM_LIMIT":  "1024Mi",

    # PVC / storage policy
    "STORAGE_CLASS_NAME": "nfs-nas-v3-expandable",
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
    "BASE_ETC_DIR": BASE_ETC_DIR,
    "PASSWD_PATH": BASE_ETC_DIR + "/passwd",
    "GROUP_PATH": BASE_ETC_DIR + "/group",
    "SHADOW_PATH": BASE_ETC_DIR + "/shadow",
    "SUDOERS_DIR": BASE_ETC_DIR + "/sudoers.d",
    "BASH_LOGOUT_PATH": BASE_ETC_DIR + "/bash.bash_logout",
    "BASHRC_PATH": BASE_ETC_DIR + "/bashrc",
})

@app.route("/health", methods=["GET"])
def health():
    """
    서버 상태 확인 API
    ---
    tags:
      - System
    responses:
      200:
        description: 서버가 살아있음
    """
    return "OK", 200


@app.route("/config", methods=["POST"])
def config():
    try:
        data = request.get_json(force=True)
        username = data.get("username")

        app.logger.info(f"/config called with body: {data}")
        app.logger.info(f"Parsed username: {username}")

        if not username:
            app.logger.warning("No username provided in /config")
            return jsonify({"config": {}, "environment": {}, "metadata": {}, "files": {}}), 200

        ns = app.config["NAMESPACE"]

        # 현재 실행 중인 Pod가 있는지 확인

        try:
            existing_pod = get_existing_pod(ns, username)
            if existing_pod:
                app.logger.info(f"Pod already exists for {username}: {existing_pod}")
        except Exception:
            app.logger.exception("Error while checking existing pod")
            return jsonify({"config": {}, "environment": {}, "metadata": {}, "files": {}}), 200


        # Spring WAS로 사용자 승인정보 요청

        try:
            was_url = app.config["WAS_URL_TEMPLATE"].format(username=username)
            app.logger.debug(f"Requesting user info from WAS: {was_url}")
            was_response = requests.get(was_url, timeout=app.config["HTTP_TIMEOUT_SEC"])
            was_response.raise_for_status()
            user_info = was_response.json()
            app.logger.debug(f"WAS response for {username}: {user_info}")
        except Exception:
            app.logger.exception("Failed to fetch user info from WAS")
            return jsonify({"config": {}, "environment": {}, "metadata": {}, "files": {}}), 200
        

        # Prometheus 노드 선택

        try:
            node_list = [node["node_name"] for node in user_info["gpu_nodes"]]
            app.logger.debug(f"Node list for Prometheus selection: {node_list}")
            best_node = select_best_node_from_prometheus(
                node_list,
                app.config["PROM_URL"],
                app.config["HTTP_TIMEOUT_SEC"]
            )
            app.logger.debug(f"Selected best node from Prometheus: {best_node}")
        except Exception:
            app.logger.exception("Failed to select best node from Prometheus")
            return jsonify({"config": {}, "environment": {}, "metadata": {}, "files": {}}), 200

        # 사용자 리소스 정보 정리
        try:
            image = load_user_image(username, user_info["image"])
            uid = user_info["uid"]
            gid_list = user_info["gid"]
            gpu_required = user_info.get("gpu_required", False)
            gpu_nodes = user_info.get("gpu_nodes", [])
            extra_ports = user_info.get("extra_ports", [])

            app.logger.info(f"[{username}] gpu_required={gpu_required}, gpu_nodes={gpu_nodes}")

            cpu_limit = app.config["DEFAULT_CPU_LIMIT"]
            memory_limit = app.config["DEFAULT_MEM_LIMIT"]
            num_gpu = 0
            for node in gpu_nodes:
                if node["node_name"] == best_node:
                    cpu_limit = node.get("cpu_limit", app.config["DEFAULT_CPU_LIMIT"])
                    memory_limit = node.get("memory_limit", app.config["DEFAULT_MEM_LIMIT"])
                    num_gpu = node.get("num_gpu", 0)
                    app.logger.info(f"[{username}] Matched node={best_node}, cpu={cpu_limit}, mem={memory_limit}, num_gpu={num_gpu}")
                    break

            pvc_name = app.config["PVC_NAME_PATTERN"].format(username=username)
        except Exception:
            app.logger.exception("Failed to parse user_info or resource limits")
            return jsonify({"config": {}, "environment": {}, "metadata": {}, "files": {}}), 200

        gpu_volume_mounts = []
        gpu_volumes = []

        if gpu_required and num_gpu > 0:
            for i in range(num_gpu):
                gpu_volume_mounts.append({
                    "name": f"nvidia{i}",
                    "mountPath": f"/dev/nvidia{i}"
                })
                gpu_volumes.append({
                    "name": f"nvidia{i}",
                    "hostPath": {
                        "path": f"/dev/nvidia{i}",
                        "type": "CharDevice"
                    }
                })

            # 보조 디바이스 추가
            for dev in app.config["NVIDIA_AUX_DEVICES"]:
                mount_name = dev.replace("-", "")
                gpu_volume_mounts.append({
                    "name": mount_name,
                    "mountPath": f"/dev/{dev}"
                })
                gpu_volumes.append({
                    "name": mount_name,
                    "hostPath": {
                        "path": f"/dev/{dev}",
                        "type": "CharDevice"
                    }
                })

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
                    "claimName": pvc_name
                }
            }
        ]

        # GPU 볼륨 추가
        volume_mounts.extend(gpu_volume_mounts)
        volumes.extend(gpu_volumes)

        # 그룹 멤버 홈 디렉토리 마운트 추가
        group_home_mounts, group_home_vols = get_group_members_home_volumes(gid_list, username)
        volume_mounts.extend(group_home_mounts)
        volumes.extend(group_home_vols)

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

        # bash 설정 파일 추가
        volume_mounts.extend([
            {"name": "bash-logout", "mountPath": f"/home/{username}/.bash_logout", "readOnly": True},
            {"name": "bashrc", "mountPath": f"/home/{username}/.bashrc", "readOnly": True}
        ])
        volumes.extend([
            {"name": "bash-logout", "hostPath": {"path": app.config["BASH_LOGOUT_PATH"], "type": "File"}},
            {"name": "bashrc", "hostPath": {"path": app.config["BASHRC_PATH"], "type": "File"}}
        ])


        try:
            spec = {
                "config": {
                    "backend": "kubernetes",
                    "kubernetes": {
                        "connection": {
                                "host": "https://kubernetes.default.svc",
                                "cacertFile": "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
                                "bearerTokenFile": "/var/run/secrets/kubernetes.io/serviceaccount/token"
                            },
                        "pod": {
                            "metadata": {
                                "name": f"containerssh-{username}",
                                "namespace": ns,
                                "labels": {
                                    "app": "containerssh-guest",
                                    "managed-by": "containerssh",
                                    "containerssh_username": username,
                                    "containerssh_pod_name": f"containerssh-{username}"
                                }
                            },
                            "spec": {
                                    "nodeName": best_node,
                                    "securityContext": {
                                        "runAsUser": uid,
                                        "runAsGroup": uid,
                                        "fsGroup": uid
                                        # "runAsGroup": gid,
                                        # "fsGroup": gid
                                    },
                                    "containers": [
                                        {
                                            "name": "shell",
                                            "image": image,
                                            "imagePullPolicy": "Never",
                                            "stdin": True,
                                            "tty": True,
                                            "ports": [
                                                {
                                                    "containerPort": port_info["internal_port"],
                                                    "protocol": "TCP",
                                                    "name": port_info.get("usage_purpose", "custom")[:15]
                                                }
                                                for port_info in extra_ports
                                            ] if extra_ports else [],
                                            "env": [
                                                {"name": "USER", "value": username},
                                                {"name": "USER_ID", "value": username},
                                                {"name": "USER_PW", "value": "1234"},
                                                {"name": "UID", "value": str(uid)},
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
                        "USER": {"value": username, "sensitive": False}
                    },
                    "metadata": {},
                    "files": {}
                }

            # NodePort Service 생성 (Pod 생성 전)
            if extra_ports:
                try:
                    create_nodeport_services(username, ns, extra_ports)
                    app.logger.info(f"Created {len(extra_ports)} NodePort services for {username}")
                except Exception as e:
                    app.logger.error(f"Failed to create NodePort services: {e}")
                    # Service 생성 실패해도 Pod는 생성되도록 계속 진행

            return jsonify(spec)
        except Exception:
            app.logger.exception("Failed to build pod spec")
            return jsonify({"config": {}, "environment": {}, "metadata": {}, "files": {}}), 200


    except Exception as e:
        app.logger.exception("Error in /config")
        return jsonify({
            "config": {},
            "environment": {},
            "metadata": {},
            "files": {}
        }), 200


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
    still_running = pod_has_process(ns, pod_name, username)

    if not still_running:
        app.logger.info(f"[{username}] No background process. Committing image before deletion...")

        # 1. Pod에서 Docker 이미지 커밋 + NFS 저장 + Redis 기록
        success = commit_and_save_user_image(username, pod_name, ns)

        if success:
            app.logger.info(f"[{username}] Image commit/save succeeded.")
        else:
            app.logger.warning(f"[{username}] Image commit/save failed.")
        
        # 2. Pod 삭제
        delete_pod(pod_name, ns)

        # 3. NodePort Service 삭제
        try:
            delete_nodeport_services(username, ns)
            app.logger.info(f"[{username}] Deleted NodePort services")
        except Exception as e:
            app.logger.error(f"[{username}] Failed to delete NodePort services: {e}")

        # 4. Redis 정리
        delete_user_status(username)
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
    pvcs = data.get("pvcs", [])
    
    # Legacy support for old format
    if not pvcs and data.get("username") and data.get("storage"):
        pvcs = [{
            "name": data["username"],
            "type": "user",
            "storage": data["storage"]
        }]
    
    if not pvcs:
        return jsonify({"results": [{"error": "pvcs list is required"}]}), 400

    results = []
    namespace = app.config["NAMESPACE"]

    try:
        try:
            k8s_config.load_incluster_config()
        except:
            k8s_config.load_kube_config()

        core_v1 = client.CoreV1Api()

        for pvc_config in pvcs:
            name = pvc_config.get("name")
            pvc_type = pvc_config.get("type", "user")
            storage_raw = pvc_config.get("storage")
            custom_pvc_name = pvc_config.get("pvc_name")

            app.logger.info(f"Processing PVC request: name={name}, type={pvc_type}, storage={storage_raw}")

            if not name or not storage_raw:
                app.logger.error(f"Missing required fields for {pvc_config}")
                results.append({"error": f"name and storage required for {pvc_config}"})
                continue

            if pvc_type not in ["user", "group"]:
                app.logger.error(f"Invalid PVC type '{pvc_type}' for {name}")
                results.append({"error": f"type must be 'user' or 'group' for {name}"})
                continue

            storage = f"{storage_raw}Gi"

            # PVC naming
            if custom_pvc_name:
                pvc_name = custom_pvc_name
            elif pvc_type == "group":
                pvc_name = f"pvc-{name}-group-share"
            else:
                pvc_name = f"pvc-{name}-share"

            app.logger.info(f"PVC name determined: {pvc_name}")

            try:
                # Check if PVC exists
                app.logger.info(f"Checking if PVC {pvc_name} already exists...")
                try:
                    existing_pvc = core_v1.read_namespaced_persistent_volume_claim(pvc_name, namespace)
                    app.logger.info(f"PVC {pvc_name} already exists, performing resize operation")
                    # Exists → resize
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
                    results.append({"status": "resized", "name": name, "type": pvc_type, "pvc_name": pvc_name, "storage": storage})
                except client.exceptions.ApiException as e:
                    if e.status != 404:
                        results.append({"error": f"Kubernetes API error for {name}: {e.body}"})
                        continue

                    # PVC doesn't exist → create
                    pvc_body = client.V1PersistentVolumeClaim(
                        metadata=client.V1ObjectMeta(
                            name=pvc_name,
                            annotations={"nfs.io/name": name, "nfs.io/type": pvc_type}
                        ),
                        spec=client.V1PersistentVolumeClaimSpec(
                            access_modes=["ReadWriteMany"],
                            resources=client.V1ResourceRequirements(
                                requests={"storage": storage}
                            ),
                            storage_class_name="nfs-nas-v3-expandable"
                        )
                    )
                    core_v1.create_namespaced_persistent_volume_claim(namespace, pvc_body)
                    app.logger.info(f"Created PVC: {pvc_name}")

                    # Wait for PVC to be bound and get the actual PV name
                    import time
                    max_wait = 30  # 30 seconds timeout
                    wait_time = 0
                    pv_name = None

                    app.logger.info(f"Waiting for PVC {pvc_name} to be bound...")
                    while wait_time < max_wait:
                        try:
                            pvc = core_v1.read_namespaced_persistent_volume_claim(pvc_name, namespace)
                            app.logger.debug(f"PVC status: {pvc.status.phase}, volume_name: {pvc.spec.volume_name}")
                            if pvc.status.phase == "Bound" and pvc.spec.volume_name:
                                pv_name = pvc.spec.volume_name
                                app.logger.info(f"PVC bound to PV: {pv_name}")
                                break
                        except Exception as e:
                            app.logger.debug(f"Error checking PVC status: {e}")
                        time.sleep(1)
                        wait_time += 1

                    # NFS storage class automatically creates directory, but we need to set proper ownership and permissions
                    if pv_name:
                        app.logger.info(f"Creating directory with PV name: {pv_name}")
                        create_directory_with_permissions(pv_name, pvc_type, username=name)
                    else:
                        app.logger.warning(f"Failed to get PV name after {max_wait}s, using fallback: {name}")
                        # Fallback to original behavior if PV name not found
                        create_directory_with_permissions(name, pvc_type)
                    
                    results.append({"status": "created", "name": name, "type": pvc_type, "pvc_name": pvc_name, "storage": storage})

            except Exception as e:
                results.append({"error": f"Failed to process {name}: {str(e)}"})

        return jsonify({"results": results})

    except Exception as e:
        return jsonify({"results": [{"error": str(e)}]}), 500



@app.route("/pvc", methods=["DELETE"])
def delete_pvc():
    """Delete PVC, PV, and associated directory
    
    Request format:
    {
        "pvcs": [
            {"name": "testuser", "type": "user"},
            {"name": "developers", "type": "group"}
        ]
    }
    
    Or legacy format:
    {"username": "testuser", "type": "user"}
    """
    data = request.get_json(force=True)
    pvcs = data.get("pvcs", [])
    
    # Legacy support for old format
    if not pvcs and data.get("username"):
        pvcs = [{
            "name": data["username"],
            "type": data.get("type", "user")
        }]
    
    if not pvcs:
        return jsonify({"results": [{"error": "pvcs list is required"}]}), 400

    results = []
    namespace = app.config["NAMESPACE"]

    try:
        try:
            k8s_config.load_incluster_config()
        except:
            k8s_config.load_kube_config()

        core_v1 = client.CoreV1Api()

        for pvc_config in pvcs:
            name = pvc_config.get("name")
            pvc_type = pvc_config.get("type", "user")
            custom_pvc_name = pvc_config.get("pvc_name")

            if not name:
                results.append({"error": f"name is required for {pvc_config}"})
                continue

            if pvc_type not in ["user", "group"]:
                results.append({"error": f"type must be 'user' or 'group' for {name}"})
                continue

            # PVC naming (same logic as create)
            if custom_pvc_name:
                pvc_name = custom_pvc_name
            elif pvc_type == "group":
                pvc_name = f"pvc-{name}-group-share"
            else:
                pvc_name = f"pvc-{name}-share"

            try:
                # Delete PVC (this also deletes the PV automatically due to reclaim policy)
                try:
                    core_v1.delete_namespaced_persistent_volume_claim(
                        name=pvc_name,
                        namespace=namespace
                    )
                    app.logger.info(f"Deleted PVC: {pvc_name}")
                except client.exceptions.ApiException as e:
                    if e.status == 404:
                        app.logger.warning(f"PVC {pvc_name} not found, skipping PVC deletion")
                    else:
                        results.append({"error": f"Failed to delete PVC {pvc_name}: {e.body}"})
                        continue

                # Delete directory
                delete_directory_if_exists(name, pvc_type)
                
                results.append({
                    "status": "deleted", 
                    "name": name, 
                    "type": pvc_type, 
                    "pvc_name": pvc_name
                })

            except Exception as e:
                results.append({"error": f"Failed to delete {name}: {str(e)}"})

        return jsonify({"results": results})

    except Exception as e:
        return jsonify({"results": [{"error": str(e)}]}), 500





accounts_bp = Blueprint("accounts", __name__)

# ---------- /etc/passwd CRUD ----------
@accounts_bp.route("/users", methods=["GET"])
def list_users():
    """List all users in the system."""
    try:
        lines = read_passwd_lines()
        users = []
        for line in lines:
            rec = parse_passwd_line(line)
            if rec:
                users.append({
                    "name": rec["name"],
                    "uid": rec["uid"],
                    "gid": rec["gid"],
                    "gecos": rec.get("gecos", ""),
                    "home": rec["home"],
                    "shell": rec["shell"]
                })
        return jsonify({"users": users}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@accounts_bp.route("/users/<username>", methods=["GET"])
def get_user(username: str):
    """Get detailed user information including group memberships."""
    try:
        # Find user in passwd
        lines = read_passwd_lines()
        user_rec = None
        for line in lines:
            rec = parse_passwd_line(line)
            if rec and rec["name"] == username:
                user_rec = rec
                break

        if not user_rec:
            return jsonify({"error": "user not found"}), 404

        # Get group memberships
        g_lines = read_group_lines()
        groups = []

        for gl in g_lines:
            grec = parse_group_line(gl)
            if not grec:
                continue

            # Primary group
            if grec["gid"] == user_rec["gid"]:
                groups.append({
                    "name": grec["name"],
                    "gid": grec["gid"],
                    "type": "primary"
                })
            # Supplementary groups
            elif username in grec.get("members", []):
                groups.append({
                    "name": grec["name"],
                    "gid": grec["gid"],
                    "type": "supplementary"
                })

        return jsonify({
            "user": {
                "name": user_rec["name"],
                "uid": user_rec["uid"],
                "gid": user_rec["gid"],
                "gecos": user_rec.get("gecos", ""),
                "home": user_rec["home"],
                "shell": user_rec["shell"]
            },
            "groups": groups
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@accounts_bp.route("/users", methods=["PUT"])
def create_user():
    """Create a new user across passwd, group, shadow, and sudoers.
    Required JSON: name, uid, gid, passwd_sha512
    Optional: gecos, primary_group_name
    Notes:
      - home is auto-set to /home/{name}
      - shell is fixed to /bin/bash
      - passwd_sha512 must be a full SHA-512 crypt string (e.g., $6$...)
    """
    data = request.get_json(force=True)
    required = ["name", "uid", "gid", "passwd_sha512"]
    missing = [k for k in required if k not in data]
    if missing:
        return jsonify({"error": f"missing fields: {', '.join(missing)}"}), 400

    name = data["name"]
    lines = read_passwd_lines()
    if any((parse_passwd_line(l) or {}).get("name") == name for l in lines):
        return jsonify({"error": "user already exists"}), 409

    entry = {
        "name": name,
        "passwd": "x",  # shadow-based auth
        "uid": int(data["uid"]),
        "gid": int(data["gid"]),
        "gecos": data.get("gecos", ""),
        "home": f"/home/{name}",
        "shell": "/bin/bash",
    }

    # 1) passwd
    lines.append(format_passwd_entry(entry))
    write_passwd_lines(lines)

    # 2) primary group ensure
    pg_name = data.get("primary_group_name", name)
    g_lines = read_group_lines()
    target_gid = int(data["gid"])
    existing = None
    for gl in g_lines:
        rec = parse_group_line(gl)
        if rec and (rec["gid"] == target_gid or rec["name"] == pg_name):
            existing = rec
            break
    if existing is None:
        new_group = {"name": pg_name, "passwd": "x", "gid": target_gid, "members": []}
        g_lines.append(format_group_entry(new_group))
        write_group_lines(g_lines)

    # 3) shadow
    today_days = int(time.time() // 86400)
    sh_lines = read_shadow_lines()
    shadow_entry = {
        "name": name,
        "passwd": data["passwd_sha512"],
        "lastchg": today_days,
        "min": 0,
        "max": 99999,
        "warn": 7,
        "inactive": "",
        "expire": "",
        "flag": "",
    }
    sh_lines.append(format_shadow_entry(shadow_entry))
    write_shadow_lines(sh_lines)

    # 4) sudoers
    ensure_sudoers_dir()
    s_path = os.path.join(app.config["SUDOERS_DIR"], name)
    tmp = s_path + ".tmp"
    with LockedFile(tmp, "w") as f:
        f.write(f"{name} ALL=(ALL) NOPASSWD:ALL\n")
    os.replace(tmp, s_path)
    os.chmod(s_path, 0o440)

    return jsonify({"status": "created", "user": entry, "group": {"name": pg_name, "gid": target_gid}, "sudoers": s_path}), 201

@accounts_bp.route("/users/<username>", methods=["DELETE"])
def delete_user(username: str):
    # Remove from /etc/passwd
    lines = read_passwd_lines()
    new_lines = []
    removed_user = None
    for line in lines:
        rec = parse_passwd_line(line)
        if rec and rec["name"] == username:
            removed_user = rec
            continue
        new_lines.append(line)
    if removed_user is None:
        return jsonify({"error": "user not found"}), 404
    write_passwd_lines(new_lines)

    # Remove from /shadow
    sh_lines = read_shadow_lines()
    sh_new = []
    for sl in sh_lines:
        srec = parse_shadow_line(sl)
        if srec and srec["name"] == username:
            continue
        sh_new.append(sl)
    write_shadow_lines(sh_new)

    # Remove sudoers file if present
    try:
        ensure_sudoers_dir()
        path = os.path.join(app.config["SUDOERS_DIR"], username)
        if os.path.exists(path):
            # lock-then-remove pattern
            with LockedFile(path, "r+") as _:
                pass
            os.remove(path)
    except Exception:
        pass

    # Clean /etc/group: remove user from all member lists; delete any group that had this user
    # (either explicitly in members or implicitly as the primary GID group) if now empty.
    g_lines = read_group_lines()
    g_new = []
    for gl in g_lines:
        grec = parse_group_line(gl)
        if not grec:
            g_new.append(gl)
            continue

        had_user_member = username in grec.get("members", [])
        is_primary_group = (removed_user is not None and grec.get("gid") == removed_user.get("gid"))

        # Remove from explicit members list
        if had_user_member:
            grec["members"] = [m for m in grec["members"] if m != username]

        # If this group had the user (explicitly or via primary gid) and is now empty, drop the group
        if (had_user_member or is_primary_group) and not grec.get("members"):
            continue

        g_new.append(format_group_entry(grec))

    write_group_lines(g_new)

    return jsonify({"status": "deleted", "user": username})

@accounts_bp.route("/groups/<groupname>", methods=["DELETE"])
def delete_group(groupname: str):
    """Delete a group from the system.
    This removes the group from /etc/group but does not affect users' primary groups.
    """
    # Check if group exists
    g_lines = read_group_lines()
    group_found = None
    new_lines = []
    
    for line in g_lines:
        rec = parse_group_line(line)
        if rec and rec["name"] == groupname:
            group_found = rec
            continue
        new_lines.append(line)
    
    if not group_found:
        return jsonify({"error": "group not found"}), 404
    
    # Check if this group is used as primary group by any user
    passwd_lines = read_passwd_lines()
    users_with_primary_gid = []
    for line in passwd_lines:
        user_rec = parse_passwd_line(line)
        if user_rec and user_rec["gid"] == group_found["gid"]:
            users_with_primary_gid.append(user_rec["name"])
    
    if users_with_primary_gid:
        return jsonify({
            "error": f"Cannot delete group {groupname}: it is the primary group for users: {', '.join(users_with_primary_gid)}"
        }), 400
    
    # Remove group
    write_group_lines(new_lines)
    
    return jsonify({
        "status": "deleted", 
        "group": groupname,
        "gid": group_found["gid"]
    })

# ----------- Group management -----------
@accounts_bp.route("/groups", methods=["PUT"])
def add_group():
    """Create a new group.
    Required JSON: name, gid
    Optional: members (array of usernames)
    """
    data = request.get_json(force=True)
    required = ["name", "gid"]
    missing = [k for k in required if k not in data]
    if missing:
        return jsonify({"error": f"missing fields: {', '.join(missing)}"}), 400

    name = data["name"]
    gid = int(data["gid"])
    members = data.get("members", [])

    # Check if group already exists
    g_lines = read_group_lines()
    for gl in g_lines:
        rec = parse_group_line(gl)
        if rec and (rec["name"] == name or rec["gid"] == gid):
            return jsonify({"error": f"group already exists (name: {rec['name']}, gid: {rec['gid']})"}), 409

    # Validate that all members exist as users
    if members:
        passwd_lines = read_passwd_lines()
        existing_users = {parse_passwd_line(l)["name"] for l in passwd_lines if parse_passwd_line(l)}
        invalid_members = [m for m in members if m not in existing_users]
        if invalid_members:
            return jsonify({"error": f"invalid members (users not found): {', '.join(invalid_members)}"}), 400

    # Create new group
    new_group = {
        "name": name,
        "passwd": "x",
        "gid": gid,
        "members": sorted(members)
    }
    
    g_lines.append(format_group_entry(new_group))
    write_group_lines(g_lines)

    return jsonify({"status": "created", "group": new_group}), 201

# ----------- Add user to supplementary groups -----------
@accounts_bp.route("/users/<username>/groups", methods=["PUT"])
def add_user_groups(username: str):
    """Add user to one or more existing groups. Body: {"groups": [...]}
    Groups must already exist. Does not touch primary group (by gid).
    """
    data = request.get_json(force=True)
    groups = data.get("groups") or []
    if not groups:
        return jsonify({"error": "'groups' list is required"}), 400

    # Verify user exists and capture their name
    user_found = False
    for line in read_passwd_lines():
        rec = parse_passwd_line(line)
        if rec and rec["name"] == username:
            user_found = True
            break
    if not user_found:
        return jsonify({"error": "user not found"}), 404

    # Update group file
    g_lines = read_group_lines()
    names = set(groups)
    updated = False
    new_lines = []
    for gl in g_lines:
        rec = parse_group_line(gl)
        if rec and rec["name"] in names:
            members = set(rec.get("members", []))
            if username not in members:
                members.add(username)
                rec["members"] = sorted(members)
                updated = True
            new_lines.append(format_group_entry(rec))
        else:
            new_lines.append(gl)

    # Ensure all requested groups existed
    existing_group_names = {parse_group_line(gl)["name"] for gl in g_lines if parse_group_line(gl)}
    missing = [g for g in groups if g not in existing_group_names]
    if missing:
        return jsonify({"error": f"groups not found: {', '.join(missing)}"}), 404

    write_group_lines(new_lines)
    return jsonify({"status": "updated", "user": username, "groups": sorted(list(names))})

# Register the blueprint under /accounts
app.register_blueprint(accounts_bp, url_prefix="/accounts")

# ==========================================
# Swagger 설정
# ==========================================
swagger_config = {
    "headers": [],
    "specs": [
        {
            "endpoint": 'apispec_1',
            "route": '/apispec_1.json',
            "rule_filter": lambda rule: True, # 모든 라우트 강제 문서화
            "model_filter": lambda tag: True,
        }
    ],
    "static_url_path": "/flasgger_static",
    "swagger_ui": True,
    "specs_route": "/apidocs/"
}

swagger_template = {
    "info": {
        "title": "GPU Server Manager API",
        "description": "Kubernetes Pod 동적 할당 및 시스템 계정 관리 API",
        "version": "1.0.0"
    },
    "definitions": {}  # 정의가 없어도 에러 안 나도록 빈 객체 추가
}

app.config['SWAGGER'] = {
    'title': 'GPU Server Manager API',
    'uiversion': 3
}

# config와 template를 모두 넣어준다.
swagger = Swagger(app, config=swagger_config, template=swagger_template)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
