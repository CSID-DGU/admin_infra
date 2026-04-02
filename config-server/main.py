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
    get_db_connection, is_pod_ready, get_existing_pod, generate_pod_name, delete_pod_util,
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
    resolve_k8s_node_name,
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
    "NAMESPACE": "ailab-infra",

    # External endpoints & timeouts
    "PROM_URL": "http://monitoring-kube-prometheus-prometheus.monitoring:9090",
    "WAS_URL_TEMPLATE": "http://admin-prod.default/api/requests/config/{username}",
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
    "ACCOUNT_FILE_SUBPATHS": [
        "passwd",
        "group",
        "shadow",
        "sudoers.d/{username}",
        "bash.bash_logout",
        "bashrc",
    ],
    # image store
    "IMAGE_STORE_DIR": "/image-store/images",

    "NVIDIA_AUX_DEVICES": [
        "nvidiactl", "nvidia-uvm", "nvidia-uvm-tools", "nvidia-modeset"
    ],
    "BASE_ETC_DIR": BASE_ETC_DIR,
    "ACCOUNT_NFS_SERVER": os.getenv("NFS_SERVER", os.getenv("NFS_ADDRESS", "")),
    "ACCOUNT_NFS_PATH": os.getenv("NFS_PATH", ""),
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

    summary: 서버 상태 확인

    responses:

      200:
        description: 서버 정상
        schema:
          type: string
          example: OK
    """
    return "OK", 200

def load_k8s():
    try:
        k8s_config.load_incluster_config()
    except:
        k8s_config.load_kube_config()

# ////////////////////// 포트 할당 //////////////////////

# reconcile 쓰로틀: 마지막 실행 시각(Unix timestamp). 앱 시작 시 0으로 초기화.
_last_reconcile_ts: float = 0.0
# 최소 실행 간격(초). allocate_nodeports() 호출 시 in-flight pod 오탐을 방지하기 위해
# 5분 간격으로 제한한다. (allocate -> Service 생성까지 통상 30초 이내이므로 충분한 여유)
_RECONCILE_INTERVAL_SEC: int = 300


def reconcile_nodeport_allocations(namespace: str) -> int:
    """
    MySQL의 nodeport_allocations 테이블과 실제 k8s NodePort Service 상태를 동기화.

    문제 상황:
        - NodePort 할당 정보는 MySQL에 저장되고, 실제 Service는 k8s(etcd)에 존재.
        - config-server를 거치지 않고 Service가 삭제되거나 (kubectl delete svc 등),
          config-server 비정상 종료로 release_nodeports()가 호출되지 않으면
          MySQL에 포트가 점유된 채로 남아 포트 고갈 발생 가능.

    동기화 방향: k8s -> MySQL  (k8s가 단일 진실 소스)
        - k8s에 실제 존재하는 NodePort Service의 pod_name 목록 조회.
        - MySQL에는 있지만 k8s에 없는 pod_name 행을 stale로 판단해 삭제.

    Args:
        namespace: NodePort Service가 존재하는 k8s 네임스페이스

    Returns:
        int: 삭제된 stale 행 수 (0이면 동기화 불필요 또는 쓰로틀로 스킵)
    """
    global _last_reconcile_ts

    # ── 쓰로틀 체크: 마지막 실행으로부터 _RECONCILE_INTERVAL_SEC 이내면 스킵 ──
    now = time.time()
    elapsed = now - _last_reconcile_ts
    if elapsed < _RECONCILE_INTERVAL_SEC:
        app.logger.debug(
            f"[RECONCILE] skipped (throttle: {int(_RECONCILE_INTERVAL_SEC - elapsed)}s remaining)"
        )
        return 0

    app.logger.info(f"[RECONCILE] start namespace={namespace}")
    # 쓰로틀 기준 시각: 성공/실패와 무관하게 "시도" 단위로 갱신한다.
    _last_reconcile_ts = time.time()

    # ── 1. k8s에서 실제 살아있는 NodePort Service의 pod_name 집합 조회 ──
    #    label_selector로 config-server가 관리하는 Service만 필터링.
    #    (app=containerssh-nodeport 라벨은 create_nodeport_services()에서 부여)
    load_k8s()  # utils.load_k8s — main.py 상단 import에서 가져옴
    v1 = client.CoreV1Api()

    try:
        services = v1.list_namespaced_service(
            namespace=namespace,
            label_selector="app=containerssh-nodeport"
        )
    except Exception as e:
        # k8s API 실패 시 reconcile 스킵. 포트 할당은 계속하고 다음 주기에 재시도.
        app.logger.warning("[RECONCILE] k8s API call failed, skipping reconcile: %s", e, exc_info=True)
        return 0

    # Service 메타데이터의 pod_name 라벨에서 살아있는 pod 이름 수집
    live_pod_names = {
        svc.metadata.labels["pod_name"]
        for svc in services.items
        if svc.metadata.labels and "pod_name" in svc.metadata.labels
    }
    app.logger.debug(f"[RECONCILE] live pods in k8s: {live_pod_names}")

    # ── 2. MySQL에서 현재 점유 중인 pod_name 목록 조회 ──
    conn = get_db_connection()
    deleted_count = 0

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT pod_name FROM nodeport_allocations")
            db_pod_names = {row[0] for row in cur.fetchall()}
            app.logger.debug(f"[RECONCILE] pods in MySQL: {db_pod_names}")

            # k8s에는 없지만 MySQL에는 남아있는 stale pod_name 계산
            stale_pod_names = db_pod_names - live_pod_names

            if not stale_pod_names:
                app.logger.info("[RECONCILE] no stale entries, DB is in sync")
                return 0

            app.logger.info(f"[RECONCILE] stale pods to remove: {stale_pod_names}")

            # stale pod의 모든 NodePort 할당 행을 삭제
            for pod in stale_pod_names:
                cur.execute(
                    "DELETE FROM nodeport_allocations WHERE pod_name=%s",
                    (pod,)
                )
                deleted_count += cur.rowcount
                app.logger.info(f"[RECONCILE] removed {cur.rowcount} rows for stale pod={pod}")

        conn.commit()
        app.logger.info(f"[RECONCILE] done, total deleted={deleted_count}")
        return deleted_count

    except Exception:
        app.logger.exception("[RECONCILE] failed, rolling back")
        conn.rollback()
        # reconcile 실패는 non-fatal. allocate_nodeports()는 stale 제거 없이 계속 진행.
        return 0
    finally:
        conn.close()


def allocate_nodeports(username, pod_name, node_name, ports):
    """
    ports:
    [
        {"internal_port": 22, "usage_purpose": "ssh"},
        {"internal_port": 8888, "usage_purpose": "jupyter"},
        ...
    ]
    """
    app.logger.info(f"[NODEPORT] allocate start username={username} pod={pod_name} node={node_name}")
    app.logger.debug(f"[NODEPORT] requested ports={ports}")

    # 포트 할당 전에 MySQL과 k8s 실제 상태를 동기화한다.
    # stale 행이 정리되어야 available 포트 계산이 정확해짐/
    # 5분 쓰로틀 적용 — in-flight pod 오탐 방지 및 k8s/DB 부하 줄어듬
    reconcile_nodeport_allocations(namespace=app.config["NAMESPACE"])

    conn = get_db_connection() #DB 연결

    try:
        with conn.cursor() as cur: #DB 커서 생성 (python pymysql 라이브러리)

            cur.execute("SELECT node_port FROM nodeport_allocations FOR UPDATE")
            used = {row[0] for row in cur.fetchall()}
            app.logger.debug(f"[NODEPORT] used ports count={len(used)}")
            available = [
                p for p in range(30000, 32768)
                if p not in used
            ]

            app.logger.debug(f"[NODEPORT] available ports count={len(available)}")

            if len(available) < len(ports):
                raise ValueError("Not enough NodePorts")

            result_ports = []

            for idx, port in enumerate(ports):
                app.logger.debug(f"[NODEPORT] assigning internal_port={port['internal_port']}")

                node_port = available[idx]
                app.logger.info(f"[NODEPORT] allocated {port['internal_port']} -> {node_port}")

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
            app.logger.info(f"[NODEPORT] allocation success total={len(result_ports)}")
            conn.commit() #Commit changes to stable storage.
            return result_ports #Return the allocated ports.

    except ValueError:
        conn.rollback()
        raise
    except Exception:
        app.logger.exception(f"[NODEPORT] allocation failed pod={pod_name}")
        conn.rollback()
        raise
    finally:
        conn.close()
    
def release_nodeports(pod_name):
    app.logger.info(f"[NODEPORT] release start pod={pod_name}")
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            app.logger.debug(f"[NODEPORT] deleting DB rows for pod={pod_name}")

            cur.execute(
                "DELETE FROM nodeport_allocations WHERE pod_name=%s",
                (pod_name,)
            )

        conn.commit()
        app.logger.info(f"[NODEPORT] release complete pod={pod_name}")
    except Exception:
        app.logger.exception(f"[NODEPORT] release failed pod={pod_name}")
        raise
    finally:
        conn.close()


@app.route("/create-pod", methods=["POST"])
def create_pod():
    """
    사용자 컨테이너 Pod 생성 API

    이 API는 Kubernetes에 사용자 Pod를 생성합니다.

    동작 과정

    1. WAS 서버에서 사용자 설정 조회
    2. GPU 노드 중 가장 적합한 노드 선택
    3. NodePort 자동 할당
    4. Pod 생성
    5. Service 생성

    ---
    tags:
    - Pod

    summary: 사용자 Pod 생성

    description: |
        특정 사용자 환경을 Kubernetes Pod로 생성합니다.

    consumes:
    - application/json

    produces:
    - application/json

    parameters:

      - in: body
        name: body
        required: true
        schema:
          $ref: '#/definitions/CreatePodRequest'

    responses:

      201:
        description: Pod 생성 성공
        schema:
          $ref: '#/definitions/CreatePodResponse'
      400:
        description: username 누락
        schema:
          $ref: '#/definitions/ErrorResponse'
      409:
        description: 동일 Pod 이미 존재
        schema:
          $ref: '#/definitions/ErrorResponse'
      500:
        description: 서버 내부 오류
        schema:
          $ref: '#/definitions/ErrorResponse'
    """
    data = request.get_json(force=True)
    username = data.get("username")

    app.logger.info(f"[CREATE POD] request received - username={username}")

    if not username:
        app.logger.warning("[CREATE POD] username missing in request")
        return jsonify({"error": "username required"}), 400

    ns = app.config["NAMESPACE"]

    try:
        # WAS 조회
        was_url = app.config["WAS_URL_TEMPLATE"].format(username=username)
        app.logger.info(f"[CREATE POD] requesting user config from WAS: {was_url}")

        user_info = requests.get(
            was_url,
            timeout=app.config["HTTP_TIMEOUT_SEC"]
        ).json()

        app.logger.debug(f"[CREATE POD] user_info received: {user_info}")

        pod_name = generate_pod_name(username)
        app.logger.info(f"[CREATE POD] generated pod_name={pod_name}")

        # pod_name 중복 확인
        load_k8s()
        v1 = client.CoreV1Api()

        try:
            v1.read_namespaced_pod(pod_name, ns)
            app.logger.warning(f"[CREATE POD] pod already exists: {pod_name}")
            return jsonify({"error": "pod already exists"}), 409
        except:
            app.logger.debug("[CREATE POD] pod does not exist yet")

        # Prometheus 기반 노드 선택
        node_list = [
            str(n["node_name"]).strip().lower()
            for n in user_info["gpu_nodes"]
            if n.get("node_name")
        ]
        app.logger.info(f"[CREATE POD] candidate nodes: {node_list}")

        best_node = select_best_node_from_prometheus(
            node_list,
            app.config["PROM_URL"],
            app.config["HTTP_TIMEOUT_SEC"]
        )
        app.logger.info(f"[CREATE POD] selected best node: {best_node}")

        # Pod spec 생성
        app.logger.info("[CREATE POD] building pod spec")

        try:
            if not best_node:
                raise ValueError(
                    "no suitable node selected (check gpu_nodes and Prometheus metrics)"
                )
            spec_wrapper, allocated_ports = build_pod_spec(
                username,
                user_info,
                best_node,
                pod_name
            )
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        app.logger.debug(f"[CREATE POD] allocated ports: {allocated_ports}")

        pod_spec = spec_wrapper["config"]["kubernetes"]["pod"]
        app.logger.info("[CREATE POD] pod spec built")

        load_k8s()
        v1 = client.CoreV1Api()

        # 실제 Pod 생성
        try:
            app.logger.info(f"[CREATE POD] creating pod in namespace={ns}")

            # 1. Pod 생성
            v1.create_namespaced_pod(
                namespace=ns,
                body=pod_spec
            )

            app.logger.info("[CREATE POD] pod creation request sent")

            # 2. Ready 대기
            app.logger.info("[CREATE POD] waiting for pod to become Ready")
            for i in range(60):
                pod = v1.read_namespaced_pod(pod_name, ns)
                if is_pod_ready(pod):
                    app.logger.info(f"[CREATE POD] pod ready after {i+1} seconds")
                    break
                time.sleep(1)
            else:
                app.logger.error("[CREATE POD] pod failed to become ready")
                app.logger.info(f"[CREATE POD] deleting failed pod: {pod_name}")
                release_nodeports(pod_name)
                v1.delete_namespaced_pod(pod_name, ns)
                return jsonify({"error": "pod failed to start"}), 500

            # 3. Pod 성공 후 Service 생성
            app.logger.info("[CREATE POD] creating NodePort services")
            create_nodeport_services(username, ns, pod_name, allocated_ports)

            app.logger.info("[CREATE POD] services created successfully")

        except Exception as e:
            app.logger.error(f"[CREATE POD] pod creation failed: {str(e)}")
            release_nodeports(pod_name)
            try:
                v1.delete_namespaced_pod(pod_name, ns)
                app.logger.warning("[CREATE POD] failed pod deleted")
            except:
                app.logger.warning("[CREATE POD] cleanup pod deletion failed")
            raise

        app.logger.info(f"[CREATE POD] success - pod={pod_name}, node={best_node}")

        return jsonify({
            "status": "created",
            "node": best_node,
            "pod_name": pod_name,
            "ports": allocated_ports
        }), 201

    except Exception as e:
        app.logger.exception("[CREATE POD] unexpected error")
        return jsonify({"error": str(e)}), 500

def build_pod_spec(
    username: str,
    user_info: dict,
    target_node: str,
    pod_name: str
):
    app.logger.info(f"[POD SPEC] start user={username} node={target_node}")
    app.logger.debug(f"[POD SPEC] user_info={user_info}")
    ns = app.config["NAMESPACE"]

    canonical = resolve_k8s_node_name(target_node)
    if not canonical:
        raise ValueError(f"unknown kubernetes node: {target_node!r}")
    if canonical != target_node:
        app.logger.info(
            f"[POD SPEC] nodeName will use canonical {canonical!r} (was {target_node!r})"
        )
    target_node = canonical

    image = load_user_image(username, user_info["image"])
    uid = user_info["uid"]
    gid_list = user_info["gid"]
    gpu_nodes = user_info.get("gpu_nodes", [])
    
    # 기본 포트
    ports = [
        {"internal_port": 22, "usage_purpose": "ssh"},
        {"internal_port": 8888, "usage_purpose": "jupyter"},
    ]
    app.logger.debug(f"[POD SPEC] base ports={ports}")

    # WAS 추가 포트
    ports.extend(user_info.get("additional_ports", []))
    app.logger.info(f"[POD SPEC] final ports={ports}")
    # 포트 할당
    allocated_ports = allocate_nodeports(
        username=username,
        pod_name=pod_name,
        node_name=target_node,
        ports=ports
    )
    try:
        app.logger.info(f"[POD SPEC] allocated_ports={allocated_ports}")
        cpu_limit = app.config["DEFAULT_CPU_LIMIT"]
        memory_limit = app.config["DEFAULT_MEM_LIMIT"]
        num_gpu = 0
    
        tn_key = target_node.lower()
        for node in gpu_nodes:
            if (node.get("node_name") or "").lower() == tn_key:
                cpu_limit = node.get("cpu_limit", cpu_limit)
                memory_limit = node.get("memory_limit", memory_limit)
                num_gpu = node.get("num_gpu", 0)
                break
    
        app.logger.info(f"[POD SPEC] resources cpu={cpu_limit} mem={memory_limit} gpu={num_gpu}")
    
        pvc_name = app.config["PVC_NAME_PATTERN"].format(username=username)
        app.logger.debug(f"[POD SPEC] pvc_name={pvc_name}")
    
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
    
        app.logger.debug(f"[POD SPEC] volume_mounts={len(volume_mounts)} volumes={len(volumes)}")
    
        group_mounts, group_vols = get_group_members_home_volumes(gid_list, username)
        volume_mounts.extend(group_mounts)
        volumes.extend(group_vols)
    # 계정 파일 마운트 -> NFS 마운트 대체
        account_file_mounts = []
        # fallback 세팅
        for sub in app.config["ACCOUNT_FILE_SUBPATHS"]:
            sub_fmt = sub.format(username=username)
            if sub.startswith("sudoers.d/"):
                mount_path = f"/etc/sudoers.d/{username}"
            elif sub == "bash.bash_logout":
                mount_path = f"/home/{username}/.bash_logout"
            elif sub == "bashrc":
                mount_path = f"/home/{username}/.bashrc"
            else:
                mount_path = f"/etc/{sub_fmt}"
            account_file_mounts.append({
                "name": "account-files",
                "mountPath": mount_path,
                "subPath": sub_fmt,
                "readOnly": True
            })

        account_nfs_server = app.config["ACCOUNT_NFS_SERVER"]
        account_nfs_path = app.config["ACCOUNT_NFS_PATH"]
        if account_nfs_server and account_nfs_path:
            volume_mounts.extend(account_file_mounts)
            volumes.append({
                "name": "account-files",
                "nfs": {
                    "server": account_nfs_server,
                    "path": account_nfs_path,
                    "readOnly": True
                }
            })
        else:
            app.logger.warning(
                "[POD SPEC] NFS_SERVER/NFS_PATH missing, falling back to legacy hostPath /etc mounts"
            )
            legacy_etc_mounts = []
            for sub in app.config["ACCOUNT_FILE_SUBPATHS"]:
                sub_fmt = sub.format(username=username)
                if sub.startswith("sudoers.d/"):
                    mount_path = f"/etc/sudoers.d/{username}"
                    source_subpath = sub_fmt
                elif sub == "bash.bash_logout":
                    mount_path = f"/home/{username}/.bash_logout"
                    source_subpath = "bash.bash_logout"
                elif sub == "bashrc":
                    mount_path = f"/home/{username}/.bashrc"
                    source_subpath = "bashrc"
                else:
                    mount_path = f"/etc/{sub_fmt}"
                    source_subpath = sub_fmt
                legacy_etc_mounts.append({
                    "name": "host-etc",
                    "mountPath": mount_path,
                    "subPath": source_subpath,
                    "readOnly": True
                })

            volume_mounts.extend(legacy_etc_mounts)
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
        app.logger.info(f"[POD SPEC] complete pod_name={pod_name}")
        return spec, allocated_ports
    except Exception:
        app.logger.warning(
            "[POD SPEC] failed after nodeport allocation; releasing rows pod=%s",
            pod_name,
        )
        release_nodeports(pod_name)
        raise

# //////////////////////// Pod 삭제 //////////////////////

@app.route("/delete-pod", methods=["POST"])
def delete_pod():
    """
    사용자 Pod 삭제 API

    특정 Pod를 삭제하고 다음 리소스를 정리합니다.

    - Kubernetes Pod
    - NodePort Service
    - NodePort DB allocation

    ---
    tags:
    - Pod

    summary: 사용자 Pod 삭제

    consumes:
    - application/json

    parameters:

      - in: body
        name: body
        required: true
        schema:
          $ref: '#/definitions/DeletePodRequest'

    responses:

      200:
        description: Pod 삭제 성공
        schema:
          type: object
          properties:
            status:
              type: string
              example: deleted
      400:
        description: 잘못된 요청
        schema:
          $ref: '#/definitions/ErrorResponse'
      500:
        description: 삭제 실패
        schema:
          $ref: '#/definitions/ErrorResponse'
    """

    data = request.get_json(force=True)
    pod_name = data.get("pod_name")

    app.logger.info(f"[DELETE POD] request received - pod_name={pod_name}")

    if not pod_name:
        app.logger.warning("[DELETE POD] pod_name missing")
        return jsonify({"error": "pod_name required"}), 400

    ns = app.config["NAMESPACE"]

    try:
        if not pod_name.startswith("containerssh-"):
            app.logger.warning(f"[DELETE POD] invalid pod_name format: {pod_name}")
            return jsonify({"error": "invalid pod_name"}), 400

        rest = pod_name[len("containerssh-"):]
        username = rest.rsplit("-", 1)[0]

        app.logger.info(f"[DELETE POD] parsed username={username}")
        app.logger.info("[DELETE POD] deleting NodePort services")
        delete_nodeport_services(pod_name, ns)
        app.logger.info("[DELETE POD] releasing NodePort allocations")
        release_nodeports(pod_name)

        app.logger.info(f"[DELETE POD] deleting pod from namespace={ns}")
        load_k8s()
        v1 = client.CoreV1Api()
        v1.delete_namespaced_pod(pod_name, ns)
        app.logger.info(f"[DELETE POD] pod deleted successfully: {pod_name}")

        return jsonify({"status": "deleted"})

    except Exception as e:
        app.logger.exception("[DELETE POD] deletion failed")
        return jsonify({"error": str(e)}), 500

def _migrate_internal(data):

    load_k8s()
    v1 = client.CoreV1Api()

    username = data.get("username")
    nodes = data.get("nodes")  # resource group에 속한 node_id 목록
    min_ratio = data.get("min_improvement_ratio", 0.2)

    ns = app.config["NAMESPACE"]

    if nodes:
        canon = []
        for n in nodes:
            c = resolve_k8s_node_name(n)
            if not c:
                return jsonify({"error": f"unknown kubernetes node: {n!r}"}), 400
            canon.append(c)
        nodes = canon

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
    resp = requests.get(
        was_url,
        timeout=app.config["HTTP_TIMEOUT_SEC"]
    )

    app.logger.info(f"[MIGRATE] WAS status={resp.status_code}")
    app.logger.debug(f"[MIGRATE] WAS body={resp.text}")

    user_info = resp.json()

    # 5. 기존 Pod 이미지 저장
    ok = commit_and_save_user_image(username, old_pod_name, ns)
    if not ok:
        return jsonify({
            "error": "image_commit_failed"
        }), 500

    # 6. 새 Pod 이름 생성
    new_pod_name = generate_pod_name(username)

    # 7. 새 노드에서 Pod 재생성
    try:
        spec_wrapper, allocated_ports = build_pod_spec(
            username,
            user_info,
            best_node,
            new_pod_name
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

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
        # 새 Pod 실패 -> 정리 후 종료
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
    delete_pod_util(old_pod_name, ns)

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
    Pod GPU 노드 마이그레이션

    현재 실행 중인 사용자 Pod를 더 성능이 좋은 GPU 노드로 이동합니다.

    동작 순서

    1. 현재 Pod 조회
    2. GPU score 계산
    3. 더 좋은 노드 존재 시 마이그레이션
    4. 기존 Pod commit
    5. 새로운 Pod 생성
    6. 기존 Pod 삭제

    ---
    tags:
    - Migration

    summary: GPU 노드 마이그레이션

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
              description: 사용자 이름
              example: alice
            nodes:
              type: array
              items:
                type: string
              example:
                - gpu-node-1
                - gpu-node-2
            min_improvement_ratio:
              type: number
              example: 0.2

    responses:

      200:
        description: 마이그레이션 성공 또는 skip
      400:
        description: 잘못된 요청
      404:
        description: 실행 중 Pod 없음
      500:
        description: 서버 오류
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

    summary: PVC 생성 또는 확장

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
                    enum:
                      - user
                      - group
                  storage:
                    type: integer
                    example: 50

    responses:

      200:
        description: PVC 처리 결과
      400:
        description: 잘못된 요청
      500:
        description: 서버 오류
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
                    # Exists -> resize
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
      500:
        description: 서버 오류
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
    """
    사용자 목록 조회

    ---
    tags:
    - Accounts

    summary: 시스템 사용자 목록

    responses:

      200:
        description: 사용자 목록 반환
      500:
        description: 서버 오류
    """
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
    """
    특정 사용자 상세 정보 조회

    사용자의 기본 계정 정보와 소속 그룹 정보를 반환합니다.

    조회 정보

    - UID
    - GID
    - 홈 디렉토리
    - 쉘
    - primary group
    - supplementary groups

    ---
    tags:
    - Accounts

    summary: 사용자 상세 정보 조회

    parameters:

      - in: path
        name: username
        required: true
        type: string
        description: 조회할 사용자 이름
        example: user2100

    responses:

      200:
        description: 사용자 정보 반환
        schema:
          type: object
          properties:
            user:
              type: object
              properties:
                name:
                  type: string
                  example: user2100
                uid:
                  type: integer
                  example: 2100
                gid:
                  type: integer
                  example: 2100
                home:
                  type: string
                  example: /home/user2100
                shell:
                  type: string
                  example: /bin/bash
            groups:
              type: array
              items:
                type: object
                properties:
                  name:
                    type: string
                    example: developers
                  gid:
                    type: integer
                    example: 3001
                  type:
                    type: string
                    example: supplementary
      404:
        description: 사용자 없음
        schema:
          $ref: '#/definitions/ErrorResponse'
      500:
        description: 서버 오류
        schema:
          $ref: '#/definitions/ErrorResponse'
    """
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
    """
    사용자 생성 API

    시스템에 새로운 Linux 사용자를 생성합니다.

    생성 대상 파일

    - /etc/passwd
    - /etc/shadow
    - /etc/group
    - /etc/sudoers.d/

    ---
    tags:
    - Accounts

    summary: 사용자 생성

    consumes:
    - application/json

    parameters:

      - in: body
        name: body
        required: true
        schema:
          type: object
          required:
            - name
            - uid
            - gid
            - passwd_sha512
          properties:
            name:
              type: string
              description: 사용자 이름
              example: user2100
            uid:
              type: integer
              description: 사용자 UID
              example: 2100
            gid:
              type: integer
              description: 기본 그룹 GID
              example: 2100
            passwd_sha512:
              type: string
              description: SHA-512 해시 패스워드
              example: "$6$hash..."
            gecos:
              type: string
              example: "GPU User"
            primary_group_name:
              type: string
              example: user2100

    responses:

      201:
        description: 사용자 생성 성공
      400:
        description: 필수 필드 누락
      409:
        description: 사용자 이미 존재
      500:
        description: 서버 오류
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
    """
    사용자 삭제 API

    다음 정보를 제거합니다.

    - /etc/passwd
    - /etc/shadow
    - /etc/group
    - /etc/sudoers.d/

    ---
    tags:
    - Accounts

    summary: 사용자 삭제

    parameters:

      - in: path
        name: username
        required: true
        type: string
        example: user2100

    responses:

      200:
        description: 삭제 성공
        schema:
          type: object
          properties:
            status:
              type: string
              example: deleted
      404:
        description: 사용자 없음
      500:
        description: 서버 오류
    """
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
    """
    그룹 삭제 API

    특정 Linux 그룹을 삭제합니다.

    주의

    - 해당 그룹이 사용자 primary group이면 삭제 불가

    ---
    tags:
    - Accounts

    summary: 그룹 삭제

    parameters:

      - in: path
        name: groupname
        required: true
        type: string
        example: developers

    responses:

      200:
        description: 삭제 성공
      400:
        description: primary group 사용 중
      404:
        description: 그룹 없음
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
    """
    그룹 생성 API

    시스템에 새로운 Linux 그룹을 생성합니다.

    ---
    tags:
    - Accounts

    summary: 그룹 생성

    consumes:
    - application/json

    parameters:

      - in: body
        name: body
        required: true
        schema:
          type: object
          required:
            - name
            - gid
          properties:
            name:
              type: string
              example: developers
            gid:
              type: integer
              example: 3001
            members:
              type: array
              items:
                type: string
              example:
                - user2100
                - user2101

    responses:

      201:
        description: 그룹 생성 성공
      400:
        description: 잘못된 요청
      409:
        description: 그룹 이미 존재
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
    """
    사용자 보조 그룹 추가 API

    특정 사용자를 하나 이상의 supplementary group에 추가합니다.

    ---
    tags:
    - Accounts

    summary: 사용자 그룹 추가

    parameters:

      - in: path
        name: username
        required: true
        type: string
        example: user2100
      - in: body
        name: body
        required: true
        schema:
          type: object
          properties:
            groups:
              type: array
              items:
                type: string
              example:
                - developers
                - ai-lab

    responses:

      200:
        description: 그룹 추가 성공
      404:
        description: 사용자 또는 그룹 없음
      400:
        description: groups 필드 누락
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
    # "definitions": {}  # 정의가 없어도 에러 안 나도록 빈 객체 추가
    "definitions": {

        "CreatePodRequest": {
            "type": "object",
            "required": ["username"],
            "properties": {
                "username": {
                    "type": "string",
                    "description": "Pod를 생성할 사용자 이름",
                    "example": "user2100"
                }
            }
        },

        "CreatePodResponse": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "example": "created"
                },
                "node": {
                    "type": "string",
                    "example": "...RTX 3080..."
                },
                "pod_name": {
                    "type": "string",
                    "example": "containerssh-user2100-1"
                },
                "ports": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "internal_port": {
                                "type": "integer",
                                "example": 22
                            },
                            "external_port": {
                                "type": "integer",
                                "example": 30001
                            },
                            "usage_purpose": {
                                "type": "string",
                                "example": "ssh"
                            }
                        }
                    }
                }
            }
        },

        "DeletePodRequest": {
            "type": "object",
            "required": ["pod_name"],
            "properties": {
                "pod_name": {
                    "type": "string",
                    "example": "containerssh-user2100-1"
                }
            }
        },

        "ErrorResponse": {
            "type": "object",
            "properties": {
                "error": {
                    "type": "string",
                    "example": "username required"
                }
            }
        }
    }
}

app.config['SWAGGER'] = {
    'title': 'GPU Server Manager API',
    'uiversion': 3
}

# config와 template를 모두 넣어준다.
swagger = Swagger(app, config=swagger_config, template=swagger_template)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)