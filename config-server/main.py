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

from utils import (
    get_db_connection, is_pod_ready, get_existing_pod, generate_pod_name, delete_pod,
    LockedFile,get_node_gpu_score,
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
    commit_and_save_user_image,
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
    # image store
    "IMAGE_STORE_DIR": "/image-store/images",

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

def load_k8s():
    try:
        k8s_config.load_incluster_config()
    except:
        k8s_config.load_kube_config()

# ////////////////////// 포트 할당 //////////////////////
def allocate_nodeports(username, pod_name, node_name, ports):
    """
    ports:
    [
        {"internal_port": 22, "usage_purpose": "ssh"},
        {"internal_port": 8888, "usage_purpose": "jupyter"},
        ...
    ]
    """

    conn = get_db_connection()

    try:
        with conn.cursor() as cur:

            cur.execute("SELECT node_port FROM nodeport_allocations FOR UPDATE")
            used = {row[0] for row in cur.fetchall()}

            available = [
                p for p in range(30000, 32768)
                if p not in used
            ]

            if len(available) < len(ports):
                raise Exception("Not enough NodePorts")

            result_ports = []

            for idx, port in enumerate(ports):

                node_port = available[idx]

                cur.execute("""
                    INSERT INTO nodeport_allocations
                    (username, pod_name, node_name, internal_port, node_port, purpose)
                    VALUES (%s,%s,%s,%s,%s,%s)
                """, (
                    username,
                    pod_name,
                    node_name,
                    port["internal_port"],
                    node_port,
                    port.get("usage_purpose", "custom")
                ))

                result_ports.append({
                    "internal_port": port["internal_port"],
                    "external_port": node_port,
                    "usage_purpose": port.get("usage_purpose", "custom")
                })

            conn.commit()
            return result_ports

    except:
        conn.rollback()
        raise
    finally:
        conn.close()
    
def release_nodeports(pod_name):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM nodeport_allocations WHERE pod_name=%s",
                (pod_name,)
            )
        conn.commit()
    finally:
        conn.close()


@app.route("/create-pod", methods=["POST"])
def create_pod():
    data = request.get_json(force=True)
    username = data.get("username")

    if not username:
        return jsonify({"error": "username required"}), 400

    ns = app.config["NAMESPACE"]

    try:
        # WAS 조회
        was_url = app.config["WAS_URL_TEMPLATE"].format(username=username)
        user_info = requests.get(
            was_url,
            timeout=app.config["HTTP_TIMEOUT_SEC"]
        ).json()

        pod_name = generate_pod_name(username)

        # pod_name 중복 확인
        load_k8s()
        v1 = client.CoreV1Api()

        try:
            v1.read_namespaced_pod(pod_name, ns)
            return jsonify({"error": "pod already exists"}), 409
        except:
            pass

        # Prometheus 기반 노드 선택
        node_list = [n["node_name"] for n in user_info["gpu_nodes"]]
        best_node = select_best_node_from_prometheus(
            node_list,
            app.config["PROM_URL"],
            app.config["HTTP_TIMEOUT_SEC"]
        )

        # Pod spec 생성
        spec_wrapper, allocated_ports = build_pod_spec(
            username,
            user_info,
            best_node,
            pod_name
        )

        pod_spec = spec_wrapper["config"]["kubernetes"]["pod"]

        load_k8s()
        v1 = client.CoreV1Api()

        # 실제 Pod 생성
        try:
            # 1. Pod 생성
            v1.create_namespaced_pod(
                namespace=ns,
                body=pod_spec
            )

            # 2. Ready 대기
            for _ in range(60):
                pod = v1.read_namespaced_pod(pod_name, ns)
                if is_pod_ready(pod):
                    break
                time.sleep(1)
            else:
                release_nodeports(pod_name)
                v1.delete_namespaced_pod(pod_name, ns)
                return jsonify({"error": "pod failed to start"}), 500

            # 3. Pod 성공 후 Service 생성
            create_nodeport_services(username, ns, pod_name, allocated_ports)

        except Exception:
            release_nodeports(pod_name)
            try:
                v1.delete_namespaced_pod(pod_name, ns)
            except:
                pass
            raise

        return jsonify({
            "status": "created",
            "node": best_node,
            "pod_name": pod_name,
            "ports": allocated_ports
        }), 201

    except Exception as e:
        app.logger.exception("create-pod failed")
        return jsonify({"error": str(e)}), 500

def build_pod_spec(
    username: str,
    user_info: dict,
    target_node: str,
    pod_name: str
):
    ns = app.config["NAMESPACE"]

    image = load_user_image(username, user_info["image"])
    uid = user_info["uid"]
    gid_list = user_info["gid"]
    gpu_nodes = user_info.get("gpu_nodes", [])
    
    # 기본 포트
    ports = [
        {"internal_port": 22, "usage_purpose": "ssh"},
        {"internal_port": 8888, "usage_purpose": "jupyter"},
    ]

    # WAS 추가 포트
    ports.extend(user_info.get("additional_ports", []))

    # 포트 할당
    allocated_ports = allocate_nodeports(
        username=username,
        pod_name=pod_name,
        node_name=target_node,
        ports=ports
    )

    cpu_limit = app.config["DEFAULT_CPU_LIMIT"]
    memory_limit = app.config["DEFAULT_MEM_LIMIT"]
    num_gpu = 0

    for node in gpu_nodes:
        if node["node_name"] == target_node:
            cpu_limit = node.get("cpu_limit", cpu_limit)
            memory_limit = node.get("memory_limit", memory_limit)
            num_gpu = node.get("num_gpu", 0)
            break

    pvc_name = app.config["PVC_NAME_PATTERN"].format(username=username)

    gpu_volume_mounts = []
    gpu_volumes = []

    if num_gpu > 0:
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

    volume_mounts = [{
        "name": "user-home",
        "mountPath": f"/home/{username}",
        "readOnly": False
    }]
    volume_mounts.append({
        "name": "image-store",
        "mountPath": "/image-store",
        "readOnly": False
    })
    volumes = [{
        "name": "user-home",
        "persistentVolumeClaim": {
            "claimName": pvc_name
        }
    }]
    volumes.append({
        "name": "image-store",
        "persistentVolumeClaim": {
            "claimName": "pvc-image-store"
        }
    })

    volume_mounts.extend(gpu_volume_mounts)
    volumes.extend(gpu_volumes)

    group_mounts, group_vols = get_group_members_home_volumes(gid_list, username)
    volume_mounts.extend(group_mounts)
    volumes.extend(group_vols)

    host_etc_mounts = []
    for sub in app.config["HOST_ETC_SUBPATHS"]:
        sub_fmt = sub.format(username=username)
        mount_path = f"/etc/sudoers.d/{username}" if sub.startswith("sudoers.d/") else f"/etc/{sub_fmt}"
        host_etc_mounts.append({
            "name": "host-etc",
            "mountPath": mount_path,
            "subPath": sub_fmt,
            "readOnly": True
        })
    volume_mounts.extend(host_etc_mounts)

    volumes.append({
        "name": "host-etc",
        "hostPath": {"path": "/etc", "type": "Directory"}
    })

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
                                "name": pod_name,
                                "namespace": ns,
                                "labels": {
                                    "app": "containerssh-guest",
                                    "managed-by": "containerssh",
                                    "containerssh_username": username,
                                    "containerssh_pod_name": pod_name
                                }
                            },
                            "spec": {
                                    "nodeName": target_node,
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
                                                    "containerPort": m["internal_port"],
                                                    "protocol": "TCP"
                                                }
                                                for m in allocated_ports
                                            ],
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

    return spec, allocated_ports

# //////////////////////// Pod 삭제 //////////////////////

@app.route("/delete-pod", methods=["POST"])
def delete_pod():

    data = request.get_json(force=True)
    pod_name = data.get("pod_name")

    if not pod_name:
        return jsonify({"error": "pod_name required"}), 400

    ns = app.config["NAMESPACE"]

    try:
        if not pod_name.startswith("containerssh-"):
            return jsonify({"error": "invalid pod_name"}), 400

        rest = pod_name[len("containerssh-"):]
        username = rest.rsplit("-", 1)[0]
        
        delete_nodeport_services(pod_name, ns)
        release_nodeports(pod_name)

        load_k8s()
        v1 = client.CoreV1Api()
        v1.delete_namespaced_pod(pod_name, ns)

        return jsonify({"status": "deleted"})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

def _migrate_internal(data):

    load_k8s()
    v1 = client.CoreV1Api()

    username = data.get("username")
    nodes = data.get("nodes")  # resource group에 속한 node_id 목록
    min_ratio = data.get("min_improvement_ratio", 0.2)

    ns = app.config["NAMESPACE"]

    # 1. 현재 Pod 확인
    old_pod_name = get_existing_pod(ns, username)
    if not old_pod_name:
        return jsonify({"error": "no running pod"}), 404

    pod = v1.read_namespaced_pod(old_pod_name, ns)
    current_node = pod.spec.node_name

    if current_node not in nodes:
        return jsonify({
            "error": "current node is not in given nodes list"
        }), 400

    candidate_nodes = [n for n in nodes if n != current_node]
    if not candidate_nodes:
        return jsonify({
            "status": "skipped",
            "reason": "no_candidate_node"
        }), 200

    # 2. GPU score 계산
    prom_url = app.config["PROM_URL"]
    timeout = app.config["HTTP_TIMEOUT_SEC"]

    current_score = get_node_gpu_score(current_node, prom_url, timeout)
    scores = {
        node: get_node_gpu_score(node, prom_url, timeout)
        for node in candidate_nodes
    }

    best_node, best_score = min(scores.items(), key=lambda x: x[1])

    # 3. 이전(migrate) 기준 판단
    if best_score > current_score * (1 - min_ratio):
        return jsonify({
            "status": "skipped",
            "reason": "no_significant_improvement",
            "current_node": current_node,
            "current_score": current_score,
            "best_candidate": best_node,
            "best_score": best_score
        }), 200

    # 4. WAS에서 사용자 정보 조회
    was_url = app.config["WAS_URL_TEMPLATE"].format(username=username)
    user_info = requests.get(
        was_url,
        timeout=app.config["HTTP_TIMEOUT_SEC"]
    ).json()

    # 5. 기존 Pod 이미지 저장
    ok = commit_and_save_user_image(username, old_pod_name, ns)
    if not ok:
        return jsonify({
            "error": "image_commit_failed"
        }), 500

    # 6. 새 Pod 이름 생성
    new_pod_name = generate_pod_name(username)

    # 7. 새 노드에서 Pod 재생성
    spec_wrapper, allocated_ports = build_pod_spec(
        username,
        user_info,
        best_node,
        new_pod_name
    )

    pod_spec = spec_wrapper["config"]["kubernetes"]["pod"]

    try:
        v1.create_namespaced_pod(namespace=ns, body=pod_spec)
    except Exception:
        release_nodeports(new_pod_name)
        raise

    # 8. Ready 대기
    for _ in range(60):
        pod = v1.read_namespaced_pod(new_pod_name, ns)
        if is_pod_ready(pod):
            break
        time.sleep(1)
    else:
        # 새 Pod 실패 → 정리 후 종료
        v1.delete_namespaced_pod(new_pod_name, ns)
        release_nodeports(new_pod_name)
        return jsonify({"error": "new pod failed to start"}), 500

    # 9. 새 Pod 성공 후 Service 생성
    try:
        create_nodeport_services(
            username,
            ns,
            new_pod_name,
            allocated_ports
        )
    except Exception:
        v1.delete_namespaced_pod(new_pod_name, ns)
        release_nodeports(new_pod_name)
        return jsonify({"error": "service creation failed"}), 500

    # 10. 기존 Pod 정리
    delete_nodeport_services(old_pod_name, ns)
    release_nodeports(old_pod_name)
    delete_pod(old_pod_name, ns)

    return jsonify({
        "status": "migrated",
        "from": current_node,
        "to": best_node,
        "new_pod": new_pod_name,
        "ports": allocated_ports
    }), 200


@app.route("/migrate", methods=["POST"])
def migrate():
    """
    실행 중인 사용자 Pod를 더 좋은 GPU 노드로 마이그레이션
    ---
    tags:
      - Migration
    consumes:
      - application/json
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          required:
            - username
            - nodes
          properties:
            username:
              type: string
              example: alice
            nodes:
              type: array
              items:
                type: string
              example: ["gpu-node-1", "gpu-node-2"]
            min_improvement_ratio:
              type: number
              example: 0.2
    responses:
      200:
        description: 마이그레이션 결과
      400:
        description: 잘못된 요청
    """
    data = request.get_json(force=True)
    username = data.get("username")
    nodes = data.get("nodes")

    if not username or not nodes or not isinstance(nodes, list):
        return jsonify({
            "error": "username and nodes(list) are required"
        }), 400

    lock_path = f"/tmp/migrate-{username}.lock"

    with LockedFile(lock_path, "w"):
        return _migrate_internal(data)



@app.route("/pvc", methods=["POST"])
def create_or_resize_pvc():
    """
    PVC 생성 또는 용량 확장 API
    ---
    tags:
      - Storage
    consumes:
      - application/json
    parameters:
      - in: body
        name: body
        schema:
          type: object
          properties:
            pvcs:
              type: array
              items:
                type: object
                properties:
                  name:
                    type: string
                    example: alice
                  type:
                    type: string
                    example: user
                  storage:
                    type: integer
                    example: 50
    responses:
      200:
        description: PVC 처리 결과 목록
      400:
        description: 잘못된 요청
    """
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
    """
    PVC 및 연결된 디렉터리 삭제 API

    - 표준 요청 형식:
      {
        "pvcs": [
          {"name": "testuser", "type": "user"},
          {"name": "developers", "type": "group"}
        ]
      }

    - Legacy 요청 형식:
      {
        "username": "testuser",
        "type": "user"
      }

    ---
    tags:
      - Storage
    consumes:
      - application/json
    parameters:
      - in: body
        name: body
        required: true
        schema:
          type: object
          properties:
            pvcs:
              type: array
              items:
                type: object
                properties:
                  name:
                    type: string
                    example: testuser
                  type:
                    type: string
                    example: user
            username:
              type: string
              example: testuser
            type:
              type: string
              example: user
    responses:
      200:
        description: 삭제 결과
      400:
        description: 잘못된 요청
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
