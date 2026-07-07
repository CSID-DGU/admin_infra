from flask import Flask, request, jsonify, Blueprint
import fcntl
import re
import time
from typing import List, Optional
from kubernetes import client, config as k8s_config, watch
import pymysql
import os
import requests
import logging, sys

from flasgger import Swagger

from dotenv import load_dotenv
load_dotenv()

import base64
import crypt
import json
import subprocess
import tempfile

from error import infra_error, k8s_error_fields

from utils import (
    get_db_connection, is_pod_ready, get_existing_pod, generate_pod_name, delete_pod_util,
    LockedFile, get_node_gpu_score,
    ensure_etc_layout, ensure_sudoers_file,
    read_passwd_lines, write_passwd_lines,
    read_group_lines, write_group_lines,
    read_shadow_lines, write_shadow_lines,
    parse_passwd_line, format_passwd_entry,
    parse_group_line, format_group_entry,
    parse_shadow_line, format_shadow_entry,
    create_user_home_directory,
    delete_user_home_directory,
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

    # NFS
    "NFS_SERVER":          os.getenv("NFS_SERVER", ""),
    "NFS_USER_SHARE_PATH": os.getenv("NFS_USER_SHARE_PATH", "/volume1/share/user"),

    # Kerberos (비어있으면 비활성)
    "KRB5_REALM":           os.getenv("KRB5_REALM", ""),
    "KRB5_KDC_HOST":        os.getenv("KRB5_KDC_HOST", ""),
    "KRB5_ADMIN_PRINCIPAL": os.getenv("KRB5_ADMIN_PRINCIPAL", ""),
    "KRB5_ADMIN_PASSWORD":  os.getenv("KRB5_ADMIN_PASSWORD", ""),

    # farm 노드 keytab/timer 자동 배포용 SSH (전용 서비스 계정)
    "FARM_SSH_USER":     os.getenv("FARM_SSH_USER", ""),
    "FARM_SSH_KEY_PATH": os.getenv("FARM_SSH_KEY_PATH", ""),
    "FARM_NODES":        json.loads(os.getenv("FARM_NODES_JSON", "[]")),

    # image store
    "IMAGE_STORE_DIR": "/image-store/images",

    "NVIDIA_AUX_DEVICES": [
        "nvidiactl", "nvidia-uvm", "nvidia-uvm-tools", "nvidia-modeset"
    ],
    "BASE_ETC_DIR": BASE_ETC_DIR,
    "SUDO_ALLOWED_COMMANDS": [
        cmd.strip() for cmd in os.getenv("SUDO_ALLOWED_COMMANDS", "").split(",") if cmd.strip()
    ],
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


def wait_for_pod_deleted(v1, pod_name, namespace, timeout_sec=60):
    """
    delete_namespaced_pod() 이후 실제 파드 삭제 완료를 watch 이벤트로 확인한다.
    """
    w = watch.Watch()
    field_selector = f"metadata.name={pod_name}"
    try:
        for event in w.stream(
            v1.list_namespaced_pod,
            namespace=namespace,
            field_selector=field_selector,
            timeout_seconds=timeout_sec,
        ):
            if event.get("type") == "DELETED":
                return True
        return False
    finally:
        w.stop()


class PodSpecBuildError(Exception):
    def __init__(self, message, progress=None):
        super().__init__(message)
        self.progress = progress or {}

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
    #    (app=ailab-nodeport 라벨은 create_nodeport_services()에서 부여)
    load_k8s()  # utils.load_k8s — main.py 상단 import에서 가져옴
    v1 = client.CoreV1Api()

    try:
        services = v1.list_namespaced_service(
            namespace=namespace,
            label_selector="app=ailab-nodeport"
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
        return jsonify(infra_error(
            "VALIDATE_REQUEST",
            "INVALID_CREATE_POD_REQUEST",
            "username required",
        )), 400

    ns = app.config["NAMESPACE"]

    def cleanup_create_failure(pod_name, v1=None, delete_services=False):
        rollback = {
            "nodeportsReleased": False,
            "podDeleted": False,
            "servicesDeleted": False,
        }

        if delete_services:
            try:
                delete_nodeport_services(pod_name, ns)
                rollback["servicesDeleted"] = True
            except Exception:
                app.logger.warning("[CREATE POD] cleanup service deletion failed", exc_info=True)

        try:
            release_nodeports(pod_name)
            rollback["nodeportsReleased"] = True
        except Exception:
            app.logger.warning("[CREATE POD] cleanup nodeport release failed", exc_info=True)

        if v1 is not None:
            try:
                v1.delete_namespaced_pod(pod_name, ns)
                rollback["podDeleted"] = True
            except client.exceptions.ApiException as e:
                if e.status == 404:
                    rollback["podDeleted"] = True
                else:
                    app.logger.warning("[CREATE POD] cleanup pod deletion failed", exc_info=True)
            except Exception:
                app.logger.warning("[CREATE POD] cleanup pod deletion failed", exc_info=True)

        return rollback

    try:
        # WAS 조회
        was_url = app.config["WAS_URL_TEMPLATE"].format(username=username)
        app.logger.info(f"[CREATE POD] requesting user config from WAS: {was_url}")

        try:
            resp = requests.get(was_url, timeout=app.config["HTTP_TIMEOUT_SEC"])
            user_info = resp.json()
        except requests.RequestException as e:
            app.logger.exception("[CREATE POD] WAS request failed")
            return jsonify(infra_error(
                "FETCH_USER_CONFIG",
                "USER_CONFIG_FETCH_FAILED",
                str(e),
            )), 502
        except ValueError as e:
            app.logger.exception("[CREATE POD] invalid WAS response")
            return jsonify(infra_error(
                "FETCH_USER_CONFIG",
                "USER_CONFIG_INVALID_RESPONSE",
                str(e),
                was_status=resp.status_code if "resp" in locals() else None,
            )), 502

        # WAS가 HTTP 200 + body {"status": 404} 형태로 유저 없음을 알리는 경우 처리
        if user_info.get("status") == 404 or resp.status_code == 404:
            app.logger.warning(f"[CREATE POD] user {username!r} not found in WAS")
            return jsonify(infra_error(
                "FETCH_USER_CONFIG",
                "USER_CONFIG_NOT_FOUND",
                f"user {username!r} not found in WAS",
                was_status=resp.status_code,
            )), 404
        if resp.status_code >= 400:
            app.logger.error(f"[CREATE POD] WAS returned {resp.status_code}")
            return jsonify(infra_error(
                "FETCH_USER_CONFIG",
                "USER_CONFIG_FETCH_FAILED",
                f"WAS returned {resp.status_code} for user {username!r}",
                was_status=resp.status_code,
            )), 502

        app.logger.debug(f"[CREATE POD] user_info received: {user_info}")

        pod_name = generate_pod_name(username)
        app.logger.info(f"[CREATE POD] generated pod_name={pod_name}")

        # pod_name 중복 확인
        try:
            load_k8s()
            v1 = client.CoreV1Api()
        except Exception as e:
            app.logger.exception("[CREATE POD] k8s client setup failed")
            return jsonify(infra_error(
                "CHECK_EXISTING_POD",
                "K8S_CLIENT_SETUP_FAILED",
                str(e),
                pod_name=pod_name,
            )), 500

        try:
            v1.read_namespaced_pod(pod_name, ns)
            app.logger.warning(f"[CREATE POD] pod already exists: {pod_name}")
            return jsonify(infra_error(
                "CHECK_EXISTING_POD",
                "POD_ALREADY_EXISTS",
                "pod already exists",
                pod_name=pod_name,
            )), 409
        except client.exceptions.ApiException as e:
            if e.status != 404:
                app.logger.exception("[CREATE POD] pod existence check failed")
                return jsonify(infra_error(
                    "CHECK_EXISTING_POD",
                    "POD_CHECK_FAILED",
                    e.body,
                    pod_name=pod_name,
                    **k8s_error_fields(e),
                )), 500
            app.logger.debug("[CREATE POD] pod does not exist yet")
        except Exception as e:
            app.logger.exception("[CREATE POD] pod existence check failed")
            return jsonify(infra_error(
                "CHECK_EXISTING_POD",
                "POD_CHECK_FAILED",
                str(e),
                pod_name=pod_name,
            )), 500

        # Prometheus 기반 노드 선택
        gpu_nodes = user_info.get("gpu_nodes", [])
        node_list = [
            str(n["node_name"]).strip().lower()
            for n in gpu_nodes
            if n.get("node_name")
        ]

        # WAS가 gpu_nodes를 반환하지 않으면 k8s Ready 워커 노드 전체로 폴백
        if not node_list:
            app.logger.warning("[CREATE POD] gpu_nodes missing from WAS — falling back to all ready worker nodes")
            try:
                load_k8s()
                _all_nodes = client.CoreV1Api().list_node().items
            except client.exceptions.ApiException as e:
                app.logger.exception("[CREATE POD] fallback node list failed")
                return jsonify(infra_error(
                    "LIST_NODES",
                    "NODE_LIST_FAILED",
                    e.body,
                    **k8s_error_fields(e),
                )), 500
            except Exception as e:
                app.logger.exception("[CREATE POD] fallback node list failed")
                return jsonify(infra_error(
                    "LIST_NODES",
                    "NODE_LIST_FAILED",
                    str(e),
                )), 500
            node_list = [
                n.metadata.name
                for n in _all_nodes
                if all(c.status == "True" for c in n.status.conditions if c.type == "Ready")
                and not any(
                    "control-plane" in (t.key or "") and t.effect == "NoSchedule"
                    for t in (n.spec.taints or [])
                )
            ]

        app.logger.info(f"[CREATE POD] candidate nodes: {node_list}")

        try:
            best_node = select_best_node_from_prometheus(
                node_list,
                app.config["PROM_URL"],
                app.config["HTTP_TIMEOUT_SEC"]
            )
        except Exception as e:
            app.logger.exception("[CREATE POD] node selection failed")
            return jsonify(infra_error(
                "SELECT_NODE",
                "NODE_SELECTION_FAILED",
                str(e),
                pod_name=pod_name,
            )), 500
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
        except PodSpecBuildError as e:
            return jsonify(infra_error(
                "BUILD_POD_SPEC",
                "POD_SPEC_BUILD_FAILED",
                str(e),
                progress=e.progress,
                pod_name=pod_name,
            )), 500
        except ValueError as e:
            return jsonify(infra_error(
                "BUILD_POD_SPEC",
                "POD_SPEC_BUILD_FAILED",
                str(e),
                rollback={"nodeportsReleased": False},
                pod_name=pod_name,
            )), 400
        except Exception as e:
            app.logger.exception("[CREATE POD] pod spec build failed")
            return jsonify(infra_error(
                "BUILD_POD_SPEC",
                "POD_SPEC_BUILD_FAILED",
                str(e),
                rollback={"nodeportsReleased": False},
                pod_name=pod_name,
            )), 500
        app.logger.debug(f"[CREATE POD] allocated ports: {allocated_ports}")

        pod_spec = spec_wrapper["config"]["kubernetes"]["pod"]
        app.logger.info("[CREATE POD] pod spec built")

        try:
            load_k8s()
            v1 = client.CoreV1Api()
        except Exception as e:
            app.logger.exception("[CREATE POD] k8s client setup failed")
            rollback = cleanup_create_failure(pod_name)
            return jsonify(infra_error(
                "CREATE_POD",
                "K8S_CLIENT_SETUP_FAILED",
                str(e),
                rollback=rollback,
                pod_name=pod_name,
            )), 500

        app.logger.info(f"[CREATE POD] creating pod in namespace={ns}")
        try:
            v1.create_namespaced_pod(
                namespace=ns,
                body=pod_spec
            )
        except client.exceptions.ApiException as e:
            app.logger.exception("[CREATE POD] pod creation failed")
            rollback = cleanup_create_failure(pod_name, v1)
            return jsonify(infra_error(
                "CREATE_POD",
                "POD_CREATE_FAILED",
                e.body,
                rollback=rollback,
                pod_name=pod_name,
                **k8s_error_fields(e),
            )), 500
        except Exception as e:
            app.logger.exception("[CREATE POD] pod creation failed")
            rollback = cleanup_create_failure(pod_name, v1)
            return jsonify(infra_error(
                "CREATE_POD",
                "POD_CREATE_FAILED",
                str(e),
                rollback=rollback,
                pod_name=pod_name,
            )), 500

        app.logger.info("[CREATE POD] pod creation request sent")

        app.logger.info("[CREATE POD] waiting for pod to become Ready")
        try:
            for i in range(60):
                pod = v1.read_namespaced_pod(pod_name, ns)
                if is_pod_ready(pod):
                    app.logger.info(f"[CREATE POD] pod ready after {i+1} seconds")
                    break
                time.sleep(1)
            else:
                app.logger.error("[CREATE POD] pod failed to become ready")
                app.logger.info(f"[CREATE POD] deleting failed pod: {pod_name}")
                rollback = cleanup_create_failure(pod_name, v1)
                return jsonify(infra_error(
                    "WAIT_POD_READY",
                    "POD_READY_TIMEOUT",
                    "pod failed to start",
                    rollback=rollback,
                    pod_name=pod_name,
                )), 500
        except client.exceptions.ApiException as e:
            app.logger.exception("[CREATE POD] pod ready check failed")
            rollback = cleanup_create_failure(pod_name, v1)
            return jsonify(infra_error(
                "WAIT_POD_READY",
                "POD_READY_CHECK_FAILED",
                e.body,
                rollback=rollback,
                pod_name=pod_name,
                **k8s_error_fields(e),
            )), 500
        except Exception as e:
            app.logger.exception("[CREATE POD] pod ready check failed")
            rollback = cleanup_create_failure(pod_name, v1)
            return jsonify(infra_error(
                "WAIT_POD_READY",
                "POD_READY_CHECK_FAILED",
                str(e),
                rollback=rollback,
                pod_name=pod_name,
            )), 500

        app.logger.info("[CREATE POD] creating NodePort services")
        try:
            create_nodeport_services(username, ns, pod_name, allocated_ports)
        except client.exceptions.ApiException as e:
            app.logger.exception("[CREATE POD] service creation failed")
            rollback = cleanup_create_failure(pod_name, v1, delete_services=True)
            return jsonify(infra_error(
                "CREATE_NODEPORT_SERVICE",
                "NODEPORT_SERVICE_CREATE_FAILED",
                e.body,
                rollback=rollback,
                pod_name=pod_name,
                **k8s_error_fields(e),
            )), 500
        except Exception as e:
            app.logger.exception("[CREATE POD] service creation failed")
            rollback = cleanup_create_failure(pod_name, v1, delete_services=True)
            return jsonify(infra_error(
                "CREATE_NODEPORT_SERVICE",
                "NODEPORT_SERVICE_CREATE_FAILED",
                str(e),
                rollback=rollback,
                pod_name=pod_name,
            )), 500

        app.logger.info("[CREATE POD] services created successfully")

        app.logger.info(f"[CREATE POD] success - pod={pod_name}, node={best_node}")

        return jsonify({
            "status": "created",
            "node": best_node,
            "pod_name": pod_name,
            "ports": allocated_ports
        }), 201

    except Exception as e:
        app.logger.exception("[CREATE POD] unexpected error")
        return jsonify(infra_error(
            "CREATE_POD",
            "CREATE_POD_FAILED",
            str(e),
        )), 500


def _normalize_gid_list(raw_gid) -> List[int]:
    if raw_gid is None:
        return []
    if isinstance(raw_gid, list):
        values = raw_gid
    else:
        values = [raw_gid]
    out = []
    for value in values:
        if isinstance(value, int):
            out.append(value)
        elif str(value).isdigit():
            out.append(int(value))
    return out


def _resolve_primary_group(username: str, gid_list: List[int]) -> tuple[int, str]:
    primary_gid = None
    for line in read_passwd_lines():
        rec = parse_passwd_line(line)
        if rec and rec["name"] == username:
            primary_gid = rec["gid"]
            break

    if primary_gid is None and gid_list:
        primary_gid = gid_list[0]

    if primary_gid is None:
        raise ValueError(f"primary gid not found for user {username!r}")

    primary_group_name = username
    for line in read_group_lines():
        rec = parse_group_line(line)
        if rec and rec["gid"] == primary_gid:
            primary_group_name = rec["name"]
            break

    return primary_gid, primary_group_name


def _build_user_groups_env(
    username: str, primary_group_name: str, primary_gid: int, gid_list: List[int]
) -> str:
    """USER_GROUPS env var 값 생성: 'primary:gid,supp1:gid1,...' 형태."""
    entries = [f"{primary_group_name}:{primary_gid}"]
    seen = {primary_gid}
    g_lines = read_group_lines()
    for gid in gid_list:
        if gid in seen:
            continue
        seen.add(gid)
        for line in g_lines:
            rec = parse_group_line(line)
            if rec and rec["gid"] == gid:
                entries.append(f"{rec['name']}:{gid}")
                break
    return ",".join(entries)


def _get_sudo_allowed_commands() -> List[str]:
    return [cmd for cmd in app.config.get("SUDO_ALLOWED_COMMANDS", []) if cmd]


def _build_sudoers_policy(username: str) -> Optional[str]:
    allowed_commands = _get_sudo_allowed_commands()
    if not allowed_commands:
        return None
    return f"{username} ALL=(ALL) PASSWD: {', '.join(allowed_commands)}\n"


def _rollback_user(name: str) -> None:
    pw_lines = read_passwd_lines()
    write_passwd_lines([l for l in pw_lines if (parse_passwd_line(l) or {}).get("name") != name])

    sh_lines = read_shadow_lines()
    write_shadow_lines([l for l in sh_lines if (parse_shadow_line(l) or {}).get("name") != name])

    g_lines = read_group_lines()
    cleaned = []
    for gl in g_lines:
        rec = parse_group_line(gl)
        if not rec:
            cleaned.append(gl)
            continue
        if name in rec["members"]:
            rec["members"] = [m for m in rec["members"] if m != name]
        if rec["name"] == name and not rec["members"]:
            continue
        cleaned.append(format_group_entry(rec))
    write_group_lines(cleaned)


def build_pod_spec(
    username: str,
    user_info: dict,
    target_node: str,
    pod_name: str
):
    app.logger.info(f"[POD SPEC] start user={username} node={target_node}")
    app.logger.debug(f"[POD SPEC] user_info={user_info}")
    ns = app.config["NAMESPACE"]

    # subPath mounts require the source files to already exist on the NFS share.
    ensure_etc_layout()

    canonical = resolve_k8s_node_name(target_node)
    if not canonical:
        raise ValueError(f"unknown kubernetes node: {target_node!r}")
    if canonical != target_node:
        app.logger.info(
            f"[POD SPEC] nodeName will use canonical {canonical!r} (was {target_node!r})"
        )
    target_node = canonical

    image = load_user_image(username, user_info["image"])

    # passwd가 uid/gid의 단일 진실 소스 — WAS 값은 무시
    passwd_rec = None
    for _line in read_passwd_lines():
        _rec = parse_passwd_line(_line)
        if _rec and _rec["name"] == username:
            passwd_rec = _rec
            break
    if passwd_rec is None:
        raise ValueError(
            f"user {username!r} not found in /etc/passwd — "
            "PUT /accounts/users로 계정을 먼저 생성하세요"
        )
    uid = passwd_rec["uid"]
    primary_gid = passwd_rec["gid"]
    primary_group_name = username
    for _line in read_group_lines():
        _rec = parse_group_line(_line)
        if _rec and _rec["gid"] == primary_gid:
            primary_group_name = _rec["name"]
            break

    # group 멤버 홈 마운트용 gid 목록: groups 배열(신규 포맷) 우선, 없으면 gid 필드
    groups_from_was = user_info.get("groups", [])
    if groups_from_was and isinstance(groups_from_was, list) and isinstance(groups_from_was[0], dict):
        gid_list = [g["gid"] for g in groups_from_was if isinstance(g, dict) and "gid" in g]
    else:
        gid_list = _normalize_gid_list(user_info.get("gid"))

    gpu_nodes = user_info.get("gpu_nodes", [])
    
    # 기본 포트
    ports = [
        {"internal_port": 22, "usage_purpose": "ssh"},
        {"internal_port": 8888, "usage_purpose": "jupyter"},
    ]
    app.logger.debug(f"[POD SPEC] base ports={ports}")

    # WAS 추가 포트
    additional_ports = user_info.get("additional_ports", [])
    ports.extend(additional_ports)
    app.logger.info(f"[POD SPEC] final ports={ports}")

    # additional_ports에 novnc 포트가 포함돼 있으면 entrypoint.sh가 noVNC를 띄우도록 ENABLE_VNC 주입
    enable_vnc = any(
        p.get("usage_purpose") in ("novnc", "vnc") or p.get("internal_port") == 6080
        for p in additional_ports
    )
    app.logger.info(f"[POD SPEC] enable_vnc={enable_vnc}")
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

        # NFS user-share 전체를 /home에 마운트 — 유저 격리는 chmod 700으로 처리
        volume_mounts = [
            {"name": "nfs-home",    "mountPath": "/home",        "readOnly": False},
            {"name": "image-store", "mountPath": "/image-store", "readOnly": False},
        ]
        volumes = [
            {
                "name": "nfs-home",
                "nfs": {
                    "server":   app.config["NFS_SERVER"],
                    "path":     app.config["NFS_USER_SHARE_PATH"],
                    "readOnly": False,
                }
            },
            {
                "name": "image-store",
                "persistentVolumeClaim": {"claimName": "pvc-image-store"}
            },
        ]

        volume_mounts.extend(gpu_volume_mounts)
        volumes.extend(gpu_volumes)

        if app.config["KRB5_REALM"]:
            # keytab은 컨테이너에 마운트하지 않는다 — farm 노드에만 배포하고 호스트가 갱신한 TGT만 공유한다.
            # 이 배포가 실패하면 예외가 아래 except로 전달되어 nodeport 롤백 + Pod 미생성으로 처리된다.
            _deploy_krb5_to_farm(username, uid, target_node)

            # rpc-gssd가 호스트에서 ccache를 읽을 수 있도록 Pod와 호스트가 /run/user/<uid> 공유
            volume_mounts.append({
                "name": "krb5-ccache",
                "mountPath": f"/run/user/{uid}",
            })
            volumes.append({
                "name": "krb5-ccache",
                "hostPath": {
                    "path": f"/run/user/{uid}",
                    "type": "DirectoryOrCreate",
                },
            })

        app.logger.debug(f"[POD SPEC] volume_mounts={len(volume_mounts)} volumes={len(volumes)}")
    
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
                                        "app": "ailab-guest",
                                        "managed-by": "ailab-infra",
                                        "username": username,
                                        "pod_name": pod_name
                                    }
                                },
                                "spec": {
                                        "nodeName": target_node,
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
                                                    {"name": "USER_GROUP", "value": primary_group_name},
                                                    {"name": "TARGET_UID", "value": str(uid)},
                                                    {"name": "TARGET_GID", "value": str(primary_gid)},
                                                    {"name": "UID", "value": str(uid)},
                                                    {"name": "GID", "value": str(primary_gid)},
                                                    {"name": "HOME", "value": f"/home/{username}"},
                                                    {"name": "SHELL", "value": "/bin/bash"},
                                                    {"name": "USER_GROUPS", "value": _build_user_groups_env(username, primary_group_name, primary_gid, gid_list)},
                                                    *([{"name": "ENABLE_VNC", "value": "true"}] if enable_vnc else []),
                                                    *([
                                                        {"name": "KRB5_REALM",          "value": app.config["KRB5_REALM"]},
                                                        {"name": "DECS_KRB5_PRINCIPAL", "value": f"{username}@{app.config['KRB5_REALM']}"},
                                                    ] if app.config["KRB5_REALM"] else []),
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
    except Exception as e:
        app.logger.warning(
            "[POD SPEC] failed after nodeport allocation; releasing rows pod=%s",
            pod_name,
        )
        rollback = {"nodeportsReleased": False}
        try:
            release_nodeports(pod_name)
            rollback["nodeportsReleased"] = True
        except Exception:
            app.logger.warning(
                "[POD SPEC] nodeport release failed during rollback pod=%s",
                pod_name,
                exc_info=True,
            )
        raise PodSpecBuildError(str(e), progress=rollback) from e

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
        return jsonify(infra_error(
            "VALIDATE_REQUEST",
            "INVALID_DELETE_POD_REQUEST",
            "pod_name required",
        )), 400

    ns = app.config["NAMESPACE"]
    rollback = {
        "servicesDeleted": False,
        "nodeportsReleased": False,
        "podDeleteRequested": False,
        "podDeleted": False,
    }

    try:
        if not pod_name.startswith("ailab-"):
            app.logger.warning(f"[DELETE POD] invalid pod_name format: {pod_name}")
            return jsonify(infra_error(
                "VALIDATE_REQUEST",
                "INVALID_POD_NAME",
                "invalid pod_name",
                rollback=rollback,
                pod_name=pod_name,
            )), 400

        rest = pod_name[len("ailab-"):]
        username = rest.rsplit("-", 1)[0]

        app.logger.info(f"[DELETE POD] parsed username={username}")
        app.logger.info("[DELETE POD] deleting NodePort services")
        try:
            delete_nodeport_services(pod_name, ns)
            rollback["servicesDeleted"] = True
        except client.exceptions.ApiException as e:
            app.logger.exception("[DELETE POD] service deletion failed")
            return jsonify(infra_error(
                "DELETE_NODEPORT_SERVICE",
                "NODEPORT_SERVICE_DELETE_FAILED",
                e.body,
                rollback=rollback,
                pod_name=pod_name,
                **k8s_error_fields(e),
            )), 500
        except Exception as e:
            app.logger.exception("[DELETE POD] service deletion failed")
            return jsonify(infra_error(
                "DELETE_NODEPORT_SERVICE",
                "NODEPORT_SERVICE_DELETE_FAILED",
                str(e),
                rollback=rollback,
                pod_name=pod_name,
            )), 500

        app.logger.info("[DELETE POD] releasing NodePort allocations")
        try:
            release_nodeports(pod_name)
            rollback["nodeportsReleased"] = True
        except Exception as e:
            app.logger.exception("[DELETE POD] nodeport release failed")
            return jsonify(infra_error(
                "RELEASE_NODEPORT",
                "NODEPORT_RELEASE_FAILED",
                str(e),
                rollback=rollback,
                pod_name=pod_name,
            )), 500

        app.logger.info(f"[DELETE POD] deleting pod from namespace={ns}")
        try:
            load_k8s()
            v1 = client.CoreV1Api()
        except Exception as e:
            app.logger.exception("[DELETE POD] k8s client setup failed")
            return jsonify(infra_error(
                "DELETE_POD",
                "K8S_CLIENT_SETUP_FAILED",
                str(e),
                rollback=rollback,
                pod_name=pod_name,
            )), 500

        pod_node_name = None
        if app.config.get("KRB5_REALM"):
            try:
                pod_node_name = v1.read_namespaced_pod(pod_name, ns).spec.node_name
            except Exception:
                app.logger.warning("[DELETE POD] pod node lookup failed, farm 정리 건너뜀: %s", pod_name, exc_info=True)

        try:
            v1.delete_namespaced_pod(pod_name, ns)
            rollback["podDeleteRequested"] = True
        except client.exceptions.ApiException as e:
            if e.status == 404:
                rollback["podDeleted"] = True
                app.logger.info("[DELETE POD] pod already absent: %s", pod_name)
                return jsonify({
                    "status": "deleted",
                    "pod_name": pod_name,
                    "already_absent": True,
                    "progress": rollback,
                }), 200
            app.logger.exception("[DELETE POD] pod deletion failed")
            return jsonify(infra_error(
                "DELETE_POD",
                "POD_DELETE_FAILED",
                e.body,
                rollback=rollback,
                pod_name=pod_name,
                **k8s_error_fields(e),
            )), 500
        except Exception as e:
            app.logger.exception("[DELETE POD] pod deletion failed")
            return jsonify(infra_error(
                "DELETE_POD",
                "POD_DELETE_FAILED",
                str(e),
                rollback=rollback,
                pod_name=pod_name,
            )), 500

        app.logger.info("[DELETE POD] waiting for pod deletion to complete")
        try:
            deleted = wait_for_pod_deleted(v1, pod_name, ns, timeout_sec=60)
        except client.exceptions.ApiException as e:
            app.logger.exception("[DELETE POD] deletion polling failed")
            return jsonify(infra_error(
                "DELETE_POD",
                "POD_DELETE_FAILED",
                e.body,
                rollback=rollback,
                pod_name=pod_name,
                **k8s_error_fields(e),
            )), 500
        except Exception as e:
            app.logger.exception("[DELETE POD] deletion polling failed")
            return jsonify(infra_error(
                "DELETE_POD",
                "POD_DELETE_FAILED",
                str(e),
                rollback=rollback,
                pod_name=pod_name,
            )), 500

        if not deleted:
            app.logger.warning("[DELETE POD] pod deletion timed out: %s", pod_name)
            return jsonify(infra_error(
                "DELETE_POD",
                "POD_DELETE_TIMEOUT",
                "pod deletion did not complete within timeout",
                rollback=rollback,
                pod_name=pod_name,
            )), 500

        rollback["podDeleted"] = True
        app.logger.info(f"[DELETE POD] pod deleted successfully: {pod_name}")

        if app.config.get("KRB5_REALM") and pod_node_name:
            try:
                _remove_krb5_from_farm(username, pod_node_name)
            except Exception as e:
                app.logger.warning(f"[DELETE POD] farm 정리 실패 (무시): {username} ← {pod_node_name} — {e}")

        return jsonify({
            "status": "deleted",
            "pod_name": pod_name,
            "progress": rollback,
        }), 200

    except Exception as e:
        app.logger.exception("[DELETE POD] deletion failed")
        return jsonify(infra_error(
            "DELETE_POD",
            "DELETE_POD_FAILED",
            str(e),
            rollback=rollback,
            pod_name=pod_name,
        )), 500

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




# ---- Kerberos KDC helpers ----

def _kadmin_run(cmd: str) -> None:
    result = subprocess.run(
        [
            "kadmin",
            "-p", app.config["KRB5_ADMIN_PRINCIPAL"],
            "-w", app.config["KRB5_ADMIN_PASSWORD"],
            "-s", app.config["KRB5_KDC_HOST"],
            "-q", cmd,
        ],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(f"kadmin 실패: {result.stderr.strip()}")

def _create_krb5_principal_and_secret(username: str) -> None:
    realm = app.config["KRB5_REALM"]
    principal = f"{username}@{realm}"
    _kadmin_run(f"addprinc -randkey {principal}")
    with tempfile.NamedTemporaryFile(suffix=".keytab", delete=False) as f:
        keytab_path = f.name
    try:
        _kadmin_run(f"ktadd -k {keytab_path} {principal}")
        with open(keytab_path, "rb") as f:
            keytab_bytes = f.read()
    finally:
        os.unlink(keytab_path)
    v1 = client.CoreV1Api()
    secret = client.V1Secret(
        metadata=client.V1ObjectMeta(
            name=f"krb5-keytab-{username}",
            namespace=app.config["NAMESPACE"],
        ),
        data={"krb5.keytab": base64.b64encode(keytab_bytes).decode()},
    )
    v1.create_namespaced_secret(namespace=app.config["NAMESPACE"], body=secret)

def _delete_krb5_principal_and_secret(username: str) -> None:
    realm = app.config["KRB5_REALM"]
    try:
        _kadmin_run(f"delprinc -force {username}@{realm}")
    except Exception as e:
        app.logger.warning(f"[KRB5] principal 삭제 실패 (무시): {e}")
    v1 = client.CoreV1Api()
    try:
        v1.delete_namespaced_secret(
            name=f"krb5-keytab-{username}",
            namespace=app.config["NAMESPACE"],
        )
    except client.exceptions.ApiException as e:
        if e.status != 404:
            raise


def _get_farm_node_info(node_name: str) -> dict:
    for node in app.config["FARM_NODES"]:
        if node["name"] == node_name:
            return node
    raise ValueError(f"unknown farm node: {node_name!r}")


def _farm_ssh(host: str, port: str, remote_command: str, stdin_data: str = "") -> str:
    """전용 서비스 계정으로 접속한다. 계정 쪽에 forced-command가 걸려 있어
    remote_command는 그대로 실행되지 않고 원격 스크립트가 참고하는 값으로만 쓰인다."""
    result = subprocess.run(
        ["ssh",
         "-i", app.config["FARM_SSH_KEY_PATH"],
         "-o", "StrictHostKeyChecking=no",
         "-o", "BatchMode=yes",
         "-p", str(port),
         f"{app.config['FARM_SSH_USER']}@{host}",
         remote_command],
        input=stdin_data,
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"farm SSH 실패 ({host}:{port}): {result.stderr.strip()}")
    return result.stdout


def _deploy_krb5_to_farm(username: str, uid: int, node_name: str) -> None:
    """k8s Secret에서 keytab을 꺼내 원격 관리 스크립트의 deploy 액션으로 전달한다.
    keytab/env 작성, timer 기동, TGT 발급 확인까지 전부 원격에서 끝난다."""
    node = _get_farm_node_info(node_name)

    v1 = client.CoreV1Api()
    secret = v1.read_namespaced_secret(
        name=f"krb5-keytab-{username}",
        namespace=app.config["NAMESPACE"],
    )
    keytab_b64 = secret.data["krb5.keytab"]

    _farm_ssh(node["host"], node["port"], f"deploy {username} {uid}", stdin_data=keytab_b64)
    app.logger.info(f"[KRB5] farm 배포 완료 + TGT 확인됨: {username} → {node_name}")


def _remove_krb5_from_farm(username: str, node_name: str) -> None:
    node = _get_farm_node_info(node_name)
    _farm_ssh(node["host"], node["port"], f"remove {username}")
    app.logger.info(f"[KRB5] farm 정리 완료: {username} ← {node_name}")


def _remove_krb5_from_all_farms(username: str) -> None:
    for node in app.config["FARM_NODES"]:
        try:
            _remove_krb5_from_farm(username, node["name"])
        except Exception as e:
            app.logger.warning(f"[KRB5] farm 정리 실패 (무시): {node['name']} — {e}")


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

def _allocate_next_uid(lines, min_uid: int = 20000) -> int:
    """관리 유저(uid >= min_uid, home=/home/) 최댓값 + 1부터 시작해
    passwd 전체에서 사용 중이지 않은 uid를 반환한다.
    시스템 계정이 중간 번호를 점유해도 건너뛰므로 충돌이 없다."""
    used_uids = {rec["uid"] for line in lines if (rec := parse_passwd_line(line))}
    managed_uids = {
        rec["uid"] for line in lines
        if (rec := parse_passwd_line(line))
        and rec["uid"] >= min_uid
        and rec.get("home", "").startswith("/home/")
    }
    candidate = max(managed_uids, default=min_uid - 1) + 1
    while candidate in used_uids:
        candidate += 1
    return candidate


def _allocate_next_gid(lines, min_gid: int = 20000) -> int:
    """group 파일 기준으로 관리 그룹용 다음 GID를 반환한다."""
    reserved_gids = {65534}
    used_gids = {
        rec["gid"]
        for line in lines
        if (rec := parse_group_line(line)) and isinstance(rec.get("gid"), int)
    }
    managed_gids = {
        gid for gid in used_gids
        if gid >= min_gid and gid not in reserved_gids
    }
    candidate = max(managed_gids, default=min_gid - 1) + 1
    while candidate in used_gids or candidate in reserved_gids:
        candidate += 1
    return candidate


@accounts_bp.route("/users", methods=["PUT"])
def create_user():
    """
    사용자 생성 API

    시스템에 새로운 Linux 사용자를 생성합니다.

    생성 대상 파일

    - /etc/passwd
    - /etc/shadow
    - /etc/group

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
            - passwd_base64
          properties:
            name:
              type: string
              description: 사용자 이름
              example: user2100
            passwd_base64:
              type: string
              description: Base64 인코딩된 평문 패스워드
              example: "cGFzc3dvcmQ="
            gecos:
              type: string
              example: "GPU User"
            primary_group_name:
              type: string
              example: user2100
            supplementary_groups:
              type: array
              description: 추가 소속 그룹 목록 (없으면 생략 가능)
              items:
                type: object
                properties:
                  name:
                    type: string
                    example: ailab
                  gid:
                    type: integer
                    example: 2001

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
    required = ["name", "passwd_base64"]
    missing = [k for k in required if k not in data]
    if missing:
        return jsonify({"error": f"missing fields: {', '.join(missing)}"}), 400

    name = data["name"]
    pg_name = data.get("primary_group_name", name)
    supp_groups = data.get("supplementary_groups", [])

    for sg in supp_groups:
        if not isinstance(sg, dict) or "name" not in sg or "gid" not in sg:
            return jsonify({"error": "supplementary_groups must be list of {name, gid}"}), 400

    try:
        plaintext_pw = base64.b64decode(data["passwd_base64"], validate=True).decode("utf-8")
    except Exception:
        return jsonify({"error": "invalid passwd_base64"}), 400

    ensure_etc_layout()

    # 1) passwd — LOCK_EX를 read부터 write까지 유지해 uid 중복 배정 방지
    uid = gid = None
    entry = None
    with LockedFile(app.config["PASSWD_PATH"], "r+") as f:
        content = f.read()
        lines = content.splitlines()

        if any((parse_passwd_line(l) or {}).get("name") == name for l in lines):
            return jsonify({"error": "user already exists"}), 409

        uid = _allocate_next_uid(lines)
        gid = uid
        app.logger.info(f"[ACCOUNTS] auto-assigned uid={uid} gid={gid} for user={name}")

        entry = {
            "name": name,
            "passwd": "x",
            "uid": uid,
            "gid": gid,
            "gecos": data.get("gecos", ""),
            "home": f"/home/{name}",
            "shell": "/bin/bash",
        }
        lines.append(format_passwd_entry(entry))
        new_content = "\n".join(lines) + "\n"
        f.seek(0)
        f.write(new_content)
        f.truncate()

    # 2) group — primary 생성 + supplementary 멤버 추가
    added_supp = []
    try:
        with LockedFile(app.config["GROUP_PATH"], "r+") as f:
            content = f.read()
            g_lines = content.splitlines()

            # primary group
            primary_exists = any(
                (parse_group_line(gl) or {}).get("gid") == gid or
                (parse_group_line(gl) or {}).get("name") == pg_name
                for gl in g_lines
            )
            if not primary_exists:
                g_lines.append(format_group_entry({"name": pg_name, "passwd": "x", "gid": gid, "members": []}))

            # supplementary groups
            for sg in supp_groups:
                sg_gid = int(sg["gid"])
                sg_name = sg["name"]
                found = False
                updated = []
                for gl in g_lines:
                    rec = parse_group_line(gl)
                    if rec and rec["gid"] == sg_gid:
                        if name not in rec["members"]:
                            rec["members"].append(name)
                        updated.append(format_group_entry(rec))
                        found = True
                    else:
                        updated.append(gl)
                g_lines = updated
                if not found:
                    g_lines.append(format_group_entry({"name": sg_name, "passwd": "x", "gid": sg_gid, "members": [name]}))
                added_supp.append({"name": sg_name, "gid": sg_gid})

            new_content = "\n".join(g_lines) + "\n"
            f.seek(0)
            f.write(new_content)
            f.truncate()
    except Exception:
        app.logger.exception("[ACCOUNTS] group write failed for user=%s, rolling back", name)
        _rollback_user(name)
        return jsonify({"error": "failed to write group"}), 500

    # 3) shadow
    try:
        passwd_sha512 = crypt.crypt(plaintext_pw, crypt.mksalt(crypt.METHOD_SHA512))

        today_days = int(time.time() // 86400)
        sh_lines = read_shadow_lines()
        shadow_entry = {
            "name": name,
            "passwd": passwd_sha512,
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
    except Exception:
        app.logger.exception("[ACCOUNTS] shadow write failed for user=%s, rolling back", name)
        _rollback_user(name)
        return jsonify({"error": "failed to write shadow"}), 500

    # 4) sudoers (로컬 호스트 관리, password-protected whitelist)
    s_path = None
    sudoers_policy = _build_sudoers_policy(name)
    if sudoers_policy:
        try:
            s_path = ensure_sudoers_file(app.config["SUDOERS_DIR"], name, sudoers_policy)
        except Exception:
            app.logger.exception("[ACCOUNTS] sudoers failed for user=%s, rolling back", name)
            _rollback_user(name)
            return jsonify({"error": "failed to create sudoers file"}), 500

    # 5) NAS SSH로 홈 디렉터리 생성
    try:
        create_user_home_directory(name, uid, gid)
    except Exception:
        app.logger.exception("[ACCOUNTS] home dir creation failed for user=%s, rolling back", name)
        _rollback_user(name)
        return jsonify(infra_error("CREATE_HOME_DIRECTORY", "NAS_SSH_FAILED", f"failed to create home directory for {name}")), 500

    # 6) Kerberos principal 생성 + keytab k8s Secret 저장
    if app.config.get("KRB5_REALM"):
        try:
            _create_krb5_principal_and_secret(name)
        except Exception:
            app.logger.exception("[ACCOUNTS] KRB5 principal creation failed for user=%s, rolling back", name)
            try:
                delete_user_home_directory(name)
            except Exception:
                pass
            _rollback_user(name)
            return jsonify(infra_error("CREATE_KRB5_PRINCIPAL", "KDC_FAILED", f"failed to create Kerberos principal for {name}")), 500

    return jsonify({
        "status": "created",
        "user": entry,
        "group": {"name": pg_name, "gid": gid},
        "supplementary_groups": added_supp,
        "sudoers": s_path,
    }), 201

@accounts_bp.route("/users/<username>", methods=["DELETE"])
def delete_user(username: str):
    """
    사용자 삭제 API

    다음 정보를 제거합니다.

    - /etc/passwd
    - /etc/shadow
    - /etc/group

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

    try:
        delete_user_home_directory(username)
    except Exception:
        app.logger.warning("[ACCOUNTS] home dir deletion failed for user=%s (account files already removed)", username, exc_info=True)

    if app.config.get("KRB5_REALM"):
        _delete_krb5_principal_and_secret(username)
        _remove_krb5_from_all_farms(username)

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
          example:
            name: developers
            members:
              - user2100
              - user2101
          properties:
            name:
              type: string
              example: developers
            gid:
              type: integer
              description: 생략 시 /kube_share/group 기준으로 자동 할당
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
        schema:
          type: object
          properties:
            group:
              type: object
              properties:
                name:
                  type: string
                  example: developers
                gid:
                  type: integer
                  example: 10001
        examples:
          application/json:
            group:
              name: developers
              gid: 10001
      400:
        description: 잘못된 요청
      409:
        description: 그룹 이미 존재
    """
    data = request.get_json(force=True)
    required = ["name"]
    missing = [k for k in required if k not in data]
    if missing:
        return jsonify({"error": f"missing fields: {', '.join(missing)}"}), 400

    name = data["name"]
    members = data.get("members", [])

    gid = None
    gid_raw = data.get("gid")
    if gid_raw not in (None, ""):
        if isinstance(gid_raw, bool):
            return jsonify({"error": "gid must be an integer"}), 400
        if isinstance(gid_raw, int):
            gid = gid_raw
        elif isinstance(gid_raw, str):
            try:
                gid = int(gid_raw)
            except ValueError:
                return jsonify({"error": "gid must be an integer"}), 400
        else:
            return jsonify({"error": "gid must be an integer"}), 400

    if not isinstance(members, list):
        return jsonify({"error": "members must be a list"}), 400

    # Validate that all members exist as users
    if members:
        passwd_lines = read_passwd_lines()
        existing_users = {parse_passwd_line(l)["name"] for l in passwd_lines if parse_passwd_line(l)}
        invalid_members = [m for m in members if m not in existing_users]
        if invalid_members:
            return jsonify({"error": f"invalid members (users not found): {', '.join(invalid_members)}"}), 400

    ensure_etc_layout()
    with LockedFile(app.config["GROUP_PATH"], "r+") as f:
        g_lines = f.read().splitlines()

        if any((parse_group_line(gl) or {}).get("name") == name for gl in g_lines):
            return jsonify({"error": f"group already exists (name: {name})"}), 409

        if gid is None:
            gid = _allocate_next_gid(g_lines)
        elif any((parse_group_line(gl) or {}).get("gid") == gid for gl in g_lines):
            return jsonify({"error": f"group already exists (gid: {gid})"}), 409

        new_group = {
            "name": name,
            "passwd": "x",
            "gid": gid,
            "members": sorted(members)
        }

        g_lines.append(format_group_entry(new_group))
        f.seek(0)
        f.write("\n".join(g_lines) + "\n")
        f.truncate()

    return jsonify({"group": {"name": name, "gid": gid}}), 201

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
                    "example": "ailab-user2100-1"
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
                    "example": "ailab-user2100-1"
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
