from flask import Flask, request, jsonify
from kubernetes import client, config as k8s_config
import os
import pymysql

from dotenv import load_dotenv
load_dotenv()

from utils import get_existing_pod   # Pod 재사용 확인
from bg_redis import save_background_status
from utils import pod_has_process, delete_pod

import logging, sys

app = Flask(__name__)

# 로그 설정
handler = logging.StreamHandler(sys.stdout)
handler.setLevel(logging.DEBUG)
formatter = logging.Formatter("[%(asctime)s] %(levelname)s in %(module)s: %(message)s")
handler.setFormatter(formatter)
app.logger.addHandler(handler)
app.logger.setLevel(logging.DEBUG)

NAMESPACE = os.getenv("NAMESPACE", "default")


@app.route("/health", methods=["GET"])
def health():
    return "OK", 200


@app.route("/config", methods=["POST"])
def config():
    try:
        app.logger.info("==== /config called ====")
        app.logger.info("Raw body: %s", request.data)
        data = request.get_json(force=True)
        app.logger.info("Parsed JSON: %s", data)
        username = data.get("username")

        if not username:
            app.logger.warning("No username in request.")
            return jsonify({"config": {}, "environment": {}, "metadata": {}, "files": {}}), 200

        # 현재 실행 중인 Pod 확인 → 있으면 attach 모드로 반환
        existing_pod = get_existing_pod(NAMESPACE, username)
        if existing_pod:
            response = {
                "config": {
                    "backend": "kubernetes",
                    "kubernetes": {
                        "connection": {
                            "host": "https://kubernetes.default.svc",
                            "cacertFile": "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
                            "bearerTokenFile": "/var/run/secrets/kubernetes.io/serviceaccount/token"
                        },
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
            }
            app.logger.info("Final /config response (attach): %s", response)
            return jsonify(response)


        # Mock 사용자 정보 (Spring WAS / Prometheus 없이 테스트용)
        user_info = {
            "username": username,
            "image": "dguailab/containerssh-guest:cuda11.8-cudnn8-dev-ubuntu22.04",
            "uid": 1001,
            "gid": 1001,
            "gpu_required": True,
            "gpu_nodes": [
                {"node_name": "farm8", "num_gpu": 4}
            ]
        }

        best_node = user_info["gpu_nodes"][0]["node_name"].lower()
        image = user_info["image"]
        uid = user_info["uid"]
        gid = user_info["gid"]

        # Pod spec 반환 (없으면 새로 생성)
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
                            "namespace": NAMESPACE,
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
                                        "requests": {"cpu": "1000m", "memory": "1024Mi"},
                                        "limits": {"cpu": "1000m", "memory": "1024Mi"}
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

    still_running = pod_has_process(pod_name, NAMESPACE, username)
    if not still_running:
        delete_pod(pod_name, NAMESPACE)
        delete_user_status(username)
        return jsonify({"status": "deleted", "username": username}), 200

    save_background_status(username, pod_name, True)
    return jsonify({"status": "background", "username": username, "has_background": True}), 200

@app.route("/pvc", methods=["POST"])
def create_or_resize_pvc():
    data = request.get_json(force=True)
    username = data.get("username")
    storage_raw = data.get("storage")

    if not username or not storage_raw:
        return jsonify({"error": "username and storage are required"}), 400

    storage = f"{storage_raw}Gi"
    pvc_name = f"pvc-{username}-share"
    namespace = NAMESPACE

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
    namespace = NAMESPACE

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
    app.run(host="0.0.0.0", port=8000, debug=True)

