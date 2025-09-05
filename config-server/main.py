from flask import Flask, request, jsonify
import fcntl
import re
import time
from typing import List, Optional
from kubernetes import client, config as k8s_config
import pymysql
import os
import requests

from dotenv import load_dotenv
load_dotenv()

import logging, sys

from bg_redis import save_background_status
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
BASE_ETC_DIR = "/home/tako8/share/kube_share"
app.config.from_mapping({
    # Namespace
    "NAMESPACE": "cssh",

    # External endpoints & timeouts
    "PROM_URL": "http://210.94.179.19:9750",
    "WAS_URL_TEMPLATE": "http://210.94.179.19:9796/requests/config/{username}",
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
})

@app.route("/health", methods=["GET"])
def health():
    return "OK", 200


@app.route("/config", methods=["POST"])
def config():
    try:
        data = request.get_json(force=True)
        username = data.get("username")

        if not username:
            app.logger.warning("No username provided in /config")
            return jsonify({"config": {}, "environment": {}, "metadata": {}, "files": {}}), 200

        ns = app.config["NAMESPACE"]

        # 현재 실행 중인 Pod가 있는지 확인

        try:
            existing_pod = get_existing_pod(ns, username)
        except Exception:
            app.logger.exception("Error while checking existing pod")
            return jsonify({"config": {}, "environment": {}, "metadata": {}, "files": {}}), 200
    
        # 1.Pod가 이미 있으면 attach

        if existing_pod:
            return jsonify({
                "config": {
                    "backend": "kubernetes",
                    "kubernetes": {
                        "connection": {
                                "host": "https://kubernetes.default.svc",
                                "cacertFile": "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
                                "bearerTokenFile": "/var/run/secrets/kubernetes.io/serviceaccount/token"                                                                            },
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
            }), 200

        # 2. Pod 없으면 새로 생성

        # 2-1. Spring WAS로 사용자 승인정보 요청

        try:
            was_url = app.config["WAS_URL_TEMPLATE"].format(username=username)
            was_response = requests.get(was_url, timeout=app.config["HTTP_TIMEOUT_SEC"])
            was_response.raise_for_status()
            user_info = was_response.json()
        except Exception:
            app.logger.exception("Failed to fetch user info from WAS")
            return jsonify({"config": {}, "environment": {}, "metadata": {}, "files": {}}), 200
        

        # 2-2. Prometheus 노드 선택

        try:
            node_list = [node["node_name"] for node in user_info["gpu_nodes"]]
            best_node = select_best_node_from_prometheus(node_list)
        except Exception:
            app.logger.exception("Failed to select best node from Prometheus")
            return jsonify({"config": {}, "environment": {}, "metadata": {}, "files": {}}), 200

        # 사용자 리소스 정보 정리
        try:
            image = user_info["image"]
            uid = user_info["uid"]
            gid = user_info["gid"]
            gpu_required = user_info.get("gpu_required", False)
            gpu_nodes = user_info.get("gpu_nodes", [])

            cpu_limit = app.config["DEFAULT_CPU_LIMIT"]
            memory_limit = app.config["DEFAULT_MEM_LIMIT"]
            num_gpu = 0
            for node in gpu_nodes:
                if node["node_name"] == best_node:
                    cpu_limit = node.get("cpu_limit", app.config["DEFAULT_CPU_LIMIT"])
                    memory_limit = node.get("memory_limit", app.config["DEFAULT_MEM_LIMIT"])
                    num_gpu = node.get("num_gpu", 0)
                    break

            pvc_name = app.config["PVC_NAME_PATTERN"].format(username=username)
        except Exception:
            app.logger.exception("Failed to parse user_info or resource limits")
            return jsonify({"config": {}, "environment": {}, "metadata": {}, "files": {}}), 200

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

            for dev in app.config["NVIDIA_AUX_DEVICES"]:
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
                                    "containerssh_username": username
                                }
                            },
                            "spec": {
                                    "nodeName": best_node,
                                    "securityContext": {
                                        "runAsUser": uid,
                                        "runAsGroup": gid,
                                        "fsGroup": gid
                                    },
                                    "containers": [
                                        {
                                            "name": "shell",
                                            "image": image,
                                            "imagePullPolicy": "Never",
                                            "stdin": True,
                                            "tty": True,
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
                                            "volumeMounts": [
                                                {"name": "user-home", "mountPath": f"/home/{username}", "readOnly": False},
                                                {"name": "nvidia0", "mountPath": "/dev/nvidia0"},
                                                {"name": "nvidia1", "mountPath": "/dev/nvidia1"},
                                                {"name": "nvidia2", "mountPath": "/dev/nvidia2"},
                                                {"name": "nvidia3", "mountPath": "/dev/nvidia3"},
                                                {"name": "nvidiactl", "mountPath": "/dev/nvidiactl"},
                                                {"name": "nvidiauvm", "mountPath": "/dev/nvidia-uvm"},
                                                {"name": "nvidiauvmtools", "mountPath": "/dev/nvidia-uvm-tools"},
                                                {"name": "nvidiamodeset", "mountPath": "/dev/nvidia-modeset"},
                                                {"name": "host-etc", "mountPath": "/etc/passwd", "subPath": "passwd", "readOnly": True},
                                                {"name": "host-etc", "mountPath": "/etc/group", "subPath": "group", "readOnly": True},
                                                {"name": "host-etc", "mountPath": "/etc/shadow", "subPath": "shadow", "readOnly": True},
                                                {"name": "host-etc", "mountPath": f"/etc/sudoers.d/{username}", "subPath": f"sudoers.d/{username}", "readOnly": True},
                                                {"name": "bash-logout", "mountPath": f"/home/{username}/.bash_logout", "readOnly": True},
                                                {"name": "bashrc", "mountPath": f"/home/{username}/.bashrc", "readOnly": True}
                                            ]
                                        }
                                    ],
                                    "volumes": [
                                        {"name": "user-home", "persistentVolumeClaim": {"claimName": f"pvc-{username}-share"}},
                                        {"name": "host-etc", "hostPath": {"path": "/etc", "type": "Directory"}},
                                        {"name": "nvidia0", "hostPath": {"path": "/dev/nvidia0", "type": "CharDevice"}},
                                        {"name": "nvidia1", "hostPath": {"path": "/dev/nvidia1", "type": "CharDevice"}},
                                        {"name": "nvidia2", "hostPath": {"path": "/dev/nvidia2", "type": "CharDevice"}},
                                        {"name": "nvidia3", "hostPath": {"path": "/dev/nvidia3", "type": "CharDevice"}},
                                        {"name": "nvidiactl", "hostPath": {"path": "/dev/nvidiactl", "type": "CharDevice"}},
                                        {"name": "nvidiauvm", "hostPath": {"path": "/dev/nvidia-uvm", "type": "CharDevice"}},
                                        {"name": "nvidiauvmtools", "hostPath": {"path": "/dev/nvidia-uvm-tools", "type": "CharDevice"}},
                                        {"name": "nvidiamodeset", "hostPath": {"path": "/dev/nvidia-modeset", "type": "CharDevice"}},
                                        {"name": "bash-logout", "hostPath": {"path": "/home/jy/admin_infra/bash_logout_test", "type": "File"}},
                                        {"name": "bashrc", "hostPath": {"path": "/home/jy/admin_infra/bashrc_test", "type": "File"}}
                                    ],
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

    storage = f"{storage_raw}Gi"
    pvc_name = f"pvc-{username}-share"
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
                access_modes=["ReadWriteMany"],
                resources=client.V1ResourceRequirements(
                    requests={"storage": storage}
                ),
                storage_class_name="nfs-nas-v3-expandable"
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

    storage = f"{storage_raw}Gi"
    pvc_name = f"pvc-{username}-share"
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


from flask import Blueprint

accounts_bp = Blueprint("accounts", __name__)

# ---------- /etc/passwd CRUD ----------
@accounts_bp.route("/adduser", methods=["POST"])
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

@accounts_bp.route("/deleteuser/<username>", methods=["POST"])
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

# ----------- Group management -----------
@accounts_bp.route("/addgroup", methods=["POST"])
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
@accounts_bp.route("/addusergroup", methods=["POST"])
def add_user_groups():
    """Add user to one or more existing groups. Body: {"username": ..., "add": [...]}
    Groups must already exist. Does not touch primary group (by gid).
    """
    data = request.get_json(force=True)
    username = data.get("username")
    if not username:
        return jsonify({"error": "username is required"}), 400
    add = data.get("add") or []
    if not add:
        return jsonify({"error": "'add' list is required"}), 400

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
    names = set(add)
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
    missing = [g for g in add if g not in existing_group_names]
    if missing:
        return jsonify({"error": f"groups not found: {', '.join(missing)}"}), 404

    write_group_lines(new_lines)
    return jsonify({"status": "updated", "added_to": sorted(list(names))})

# Register the blueprint under /accounts
app.register_blueprint(accounts_bp, url_prefix="/accounts")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
