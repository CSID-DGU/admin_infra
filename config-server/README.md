# config-server 디렉토리

ContainerSSH가 사용자별 GPU Pod를 만들고 지우는 데 필요한 Flask 기반 설정 서버입니다. Kubernetes Pod/PVC/Service 생성, NodePort 할당, Linux 계정 파일 관리, 사용자 이미지 저장/로드 상태 기록을 담당합니다.

## 파일 구성

| 파일 | 역할 | 주요 입력 | 주요 출력/효과 |
| --- | --- | --- | --- |
| `main.py` | 운영용 Flask API 서버입니다. Pod 생성/삭제/마이그레이션, PVC 생성/삭제, `/accounts` 계정 CRUD, Swagger 문서를 제공합니다. | HTTP JSON 요청, WAS 사용자 설정, Prometheus metrics, MySQL, Kubernetes API, NFS 계정 파일 | JSON API 응답, Kubernetes Pod/Service/PVC 변경, MySQL NodePort allocation 변경, NFS 계정 파일 변경 |
| `utils.py` | `main.py`가 사용하는 Kubernetes, MySQL, Docker image, 계정 파일, NFS 디렉토리 보조 함수 모음입니다. | 환경변수, Flask `current_app.config`, Kubernetes API, NFS 파일, Docker CLI | DB connection, Pod/Service 조작, 파일 읽기/쓰기, 이미지 저장/로드 metadata, PVC 디렉토리 권한 변경 |
| `bg_img_redis.py` | 사용자 이미지 저장/로드 상태를 Redis에 기록하고 조회합니다. | `REDIS_HOST`, `REDIS_PORT`, `REDIS_DB`, username, 상태값 | Redis key `img:<username>`의 JSON metadata |
| `test.py` | WAS/Prometheus 의존성을 mock 값으로 대체한 레거시/실험용 Flask 서버입니다. | HTTP JSON 요청, Kubernetes API | ContainerSSH config JSON, PVC/계정 API 응답. 일부 helper 이름은 현재 `utils.py`와 다를 수 있어 실행 전 점검이 필요합니다. |
| `Dockerfile` | config-server 운영 이미지를 빌드합니다. | 현재 디렉토리 소스, `requirements.txt` | Python 3.10 slim 기반 gunicorn 이미지 |
| `requirements.txt` | Python 런타임 의존성 목록입니다. | pip | Flask, Kubernetes client, PyMySQL, Redis, requests, flasgger, gunicorn 설치 |
| `Makefile` | Helm 배포 shortcut을 둔 파일입니다. | `make deploy`, Helm chart 경로 | config-server Helm upgrade/install 실행 |
| `base_etc/` | NFS 계정 파일이 비어 있을 때 seed로 쓰는 기본 passwd/group/shadow/bash 파일입니다. | 기본 Linux 계정 템플릿 | `/kube_share` 하위 계정 파일 초기값 |
| `Chart/` | config-server 배포용 Helm chart입니다. | Helm values | Deployment, Service, RBAC, ServiceAccount 리소스 |

## `main.py` API와 함수

| 이름 | 종류 | 역할 | 입력 | 출력/효과 |
| --- | --- | --- | --- | --- |
| `health` | route `GET /health` | 서버 상태 확인 | 없음 | `"OK"`, HTTP 200 |
| `load_k8s` | function | in-cluster config를 우선 로드하고 실패 시 kubeconfig를 로드합니다. | 없음 | Kubernetes client 설정 |
| `reconcile_nodeport_allocations` | function | MySQL의 `nodeport_allocations`와 실제 Kubernetes NodePort Service 상태를 동기화합니다. | namespace | 삭제한 stale DB row 수 |
| `allocate_nodeports` | function | 요청된 내부 포트마다 사용 가능한 NodePort를 DB row lock으로 할당합니다. | username, pod_name, node_name, port dict list | `internal_port`, `external_port`, `usage_purpose` 목록 |
| `release_nodeports` | function | 특정 Pod의 NodePort 할당 row를 삭제합니다. | pod_name | DB row 삭제 |
| `create_pod` | route `POST /create-pod` | WAS 사용자 정보를 조회하고 최적 GPU 노드를 선택해 Pod와 NodePort Service를 생성합니다. | JSON `{"username": ...}` | 201 JSON `{status,node,pod_name,ports}` 또는 오류 |
| `_normalize_gid_list` | function | 단일 gid 또는 gid 목록을 int 목록으로 정규화합니다. | raw gid 값 | `List[int]` |
| `_resolve_primary_group` | function | passwd/group 파일에서 사용자의 primary gid와 group name을 찾습니다. | username, gid list | `(primary_gid, primary_group_name)` |
| `_get_sudo_allowed_commands` | function | 앱 설정의 sudo 허용 명령 목록을 가져옵니다. | 없음 | command string list |
| `_build_sudoers_policy` | function | 사용자별 sudoers 정책 라인을 생성합니다. | username | 정책 문자열 또는 `None` |
| `_get_account_file_subpaths` | function | Pod에 mount할 계정 파일 subPath 목록을 만듭니다. | 없음 | subPath 문자열 목록 |
| `build_pod_spec` | function | ContainerSSH가 생성할 Kubernetes Pod spec과 NodePort 할당 결과를 만듭니다. | username, user_info, target_node, pod_name | ContainerSSH config wrapper dict, allocated ports |
| `delete_pod` | route `POST /delete-pod` | Pod, NodePort Service, NodePort DB row를 정리합니다. | JSON `{"pod_name": ...}` | JSON `{status:"deleted"}` |
| `_migrate_internal` | function | 현재 Pod와 후보 노드 GPU 점수를 비교하고 더 좋은 노드로 이동합니다. | request data dict | Flask JSON response |
| `migrate` | route `POST /migrate` | 사용자 Pod GPU 노드 마이그레이션을 lock으로 감싸 실행합니다. | JSON `{"username":..., "nodes":[...], "min_improvement_ratio":...}` | migrated/skipped/error JSON |
| `create_or_resize_pvc` | route `POST /pvc` | 사용자/그룹 PVC를 생성하거나 기존 PVC 용량을 확장합니다. | JSON `{"pvcs":[{"name","type","storage","pvc_name?"}]}` 또는 legacy username/storage | JSON `{results:[...]}` |
| `delete_pvc` | route `DELETE /pvc` | PVC와 연결 NFS 디렉토리를 삭제합니다. | JSON `{"pvcs":[{"name","type","pvc_name?"}]}` 또는 legacy username/type | JSON `{results:[...]}` |
| `list_users` | route `GET /accounts/users` | passwd 파일의 사용자 목록을 반환합니다. | 없음 | JSON `{users:[...]}` |
| `get_user` | route `GET /accounts/users/<username>` | 사용자 상세와 primary/supplementary group 정보를 반환합니다. | path username | JSON `{user,groups}` |
| `create_user` | route `PUT /accounts/users` | passwd/group/shadow/sudoers 파일에 사용자를 추가합니다. | JSON `name`, `uid`, `gid`, `passwd_sha512`, 선택 필드 | 201 JSON `{status,user,group,sudoers}` |
| `delete_user` | route `DELETE /accounts/users/<username>` | 사용자와 shadow/sudoers/member group 정보를 삭제합니다. | path username | JSON `{status,user}` |
| `delete_group` | route `DELETE /accounts/groups/<groupname>` | primary group으로 쓰이지 않는 그룹을 삭제합니다. | path groupname | JSON `{status,group,gid}` |
| `add_group` | route `PUT /accounts/groups` | 새 Linux group row를 추가합니다. | JSON `name`, `gid`, optional `members` | 201 JSON `{status,group}` |
| `add_user_groups` | route `PUT /accounts/users/<username>/groups` | 사용자를 보조 그룹에 추가합니다. | path username, JSON `groups` | JSON `{status,user,groups}` |

## `utils.py` 클래스와 함수

| 이름 | 종류 | 역할 | 입력 | 출력/효과 |
| --- | --- | --- | --- | --- |
| `LockedFile` | class | NFS 파일을 조작할 때 `/tmp` lock 파일로 shared/exclusive lock을 잡는 context manager입니다. | path, mode | open file object, 종료 시 unlock |
| `get_db_connection` | function | PyMySQL connection을 생성합니다. | `DB_HOST`, `DB_USER`, `DB_PASSWORD`, `DB_NAME` | transaction mode DB connection |
| `load_k8s`, `resolve_k8s_node_name`, `is_pod_ready`, `get_existing_pod`, `generate_pod_name`, `delete_pod_util` | function group | Kubernetes 설정 로드, 노드명 정규화, Pod readiness/존재 확인, Pod 이름 생성/삭제를 수행합니다. | namespace, username, pod object/name, node candidate | 정규화된 노드명, Pod명, bool, Kubernetes API 변경 |
| `create_nodeport_services`, `delete_nodeport_services` | function | 사용자 Pod별 NodePort Service를 생성/삭제합니다. | username, namespace, pod_name, port mapping list | Kubernetes Service 생성/삭제 |
| `load_user_image`, `commit_and_save_user_image` | function | 저장된 사용자 tar 이미지를 로드하거나 Pod 내부 `save_image.sh`를 실행해 이미지를 저장합니다. | username, base image, pod_name, namespace | 사용할 image name, Redis metadata, tar 이미지 저장 |
| `_local_lockfile_path` | function | NFS 경로에 대응하는 로컬 lock 파일 경로를 만듭니다. | NFS path | `/tmp/cssh_lock...` path |
| `ensure_dir`, `ensure_file`, `ensure_seeded_file`, `ensure_etc_layout`, `ensure_sudoers_dir` | function group | 계정 파일 디렉토리와 seed 파일을 준비합니다. | path, template name | 디렉토리/파일 생성 또는 초기 내용 복사 |
| `read_passwd_lines`, `write_passwd_lines`, `parse_passwd_line`, `format_passwd_entry` | function group | passwd 파일을 읽고 쓰며 행과 dict를 상호 변환합니다. | passwd lines 또는 entry dict | passwd line list 또는 formatted line |
| `read_group_lines`, `write_group_lines`, `parse_group_line`, `format_group_entry` | function group | group 파일을 읽고 쓰며 멤버 목록을 dict로 변환합니다. | group lines 또는 entry dict | group line list 또는 formatted line |
| `read_shadow_lines`, `write_shadow_lines`, `parse_shadow_line`, `format_shadow_entry` | function group | shadow 파일을 읽고 쓰며 패스워드 aging 필드를 변환합니다. | shadow lines 또는 entry dict | shadow line list 또는 formatted line |
| `create_directory_with_permissions`, `delete_directory_if_exists` | function | NFS PVC 디렉토리를 만들고 uid/gid 권한을 맞추거나 삭제합니다. | username/PV name, pvc_type, optional username | NFS 디렉토리 생성/권한 변경/삭제 |
| `get_node_gpu_score`, `select_best_node_from_prometheus` | function | Prometheus query로 GPU 노드 부하 점수를 계산하고 최적 노드를 고릅니다. | node list, Prometheus URL, timeout | score float 또는 best node |
| `get_group_members_home_volumes` | function | 같은 그룹 구성원의 홈 PVC를 읽기 전용 mount로 추가하기 위한 spec 조각을 만듭니다. | gid list, current username | `(volume_mounts, volumes)` |

## `bg_img_redis.py` 함수

| 함수 | 역할 | 입력 | 출력/효과 |
| --- | --- | --- | --- |
| `save_image_metadata` | 사용자 이미지 상태를 Redis에 저장합니다. | username, status, size_mb, version, path | 저장된 dict |
| `get_image_metadata` | 특정 사용자 이미지 metadata를 조회합니다. | username | dict 또는 `None` |
| `get_all_images` | `img:*` key 전체를 사용자명 기준 dict로 반환합니다. | 없음 | `{username: metadata}` |
| `delete_image_metadata` | 특정 사용자 이미지 metadata를 삭제합니다. | username | Redis key 삭제 |

## `test.py` 함수

| 함수 | 역할 | 입력 | 출력/효과 |
| --- | --- | --- | --- |
| `health` | mock 서버 헬스체크입니다. | 없음 | `"OK"` |
| `config` | mock 사용자 정보로 ContainerSSH Kubernetes Pod config를 반환합니다. | JSON `username` | ContainerSSH config JSON |
| `report_background` | 세션 종료 후 background process 여부를 확인해 Pod 삭제 여부를 결정합니다. | JSON `username`, `pod_name`, optional `has_background` | background/deleted JSON |
| `create_or_resize_pvc`, `resize_pvc` | 단일 사용자 PVC를 생성/확장합니다. | JSON `username`, `storage` | created/resized JSON |
| `select_best_node_from_prometheus` | 하드코딩된 Prometheus URL로 최적 노드를 고르는 실험 함수입니다. | node list | best node 또는 `None` |
| `create_user`, `delete_user`, `add_user_groups` | 레거시 `/accounts` 계정 CRUD API입니다. | JSON 또는 path username | passwd/group/shadow 파일 갱신 JSON |
