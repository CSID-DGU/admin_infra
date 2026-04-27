# config-server 디렉토리

ContainerSSH가 사용자별 GPU Pod를 만들고 지우는 데 필요한 Flask 기반 설정 서버이다. Kubernetes Pod/PVC/Service 생성, NodePort 할당, Linux 계정 파일 관리, 사용자 이미지 저장/로드 상태 기록을 담당한다.

## 파일 구성

| 파일 | 역할 | 주요 입력 | 주요 출력/효과 |
| --- | --- | --- | --- |
| `main.py` | 운영용 Flask API 서버이다. Pod 생성/삭제/마이그레이션, PVC 생성/삭제, `/accounts` 계정 CRUD, Swagger 문서를 제공한다. | HTTP JSON 요청, WAS 사용자 설정, Prometheus metrics, MySQL, Kubernetes API, NFS 계정 파일 | JSON API 응답, Kubernetes Pod/Service/PVC 변경, MySQL NodePort allocation 변경, NFS 계정 파일 변경 |
| `utils.py` | `main.py`가 사용하는 Kubernetes, MySQL, Docker image, 계정 파일, NFS 디렉토리 보조 함수 모음이다. | 환경변수, Flask `current_app.config`, Kubernetes API, NFS 파일, Docker CLI | DB connection, Pod/Service 조작, 파일 읽기/쓰기, 이미지 저장/로드 metadata, PVC 디렉토리 권한 변경 |
| `bg_img_redis.py` | 사용자 이미지 저장/로드 상태를 Redis에 기록하고 조회한다. | `REDIS_HOST`, `REDIS_PORT`, `REDIS_DB`, username, 상태값 | Redis key `img:<username>`의 JSON metadata |
| `test.py` | WAS/Prometheus 의존성을 mock 값으로 대체한 레거시/실험용 Flask 서버이다. | HTTP JSON 요청, Kubernetes API | ContainerSSH config JSON, PVC/계정 API 응답. 일부 helper 이름은 현재 `utils.py`와 다를 수 있어 실행 전 점검이 필요하다. |
| `Dockerfile` | config-server 운영 이미지를 빌드한다. | 현재 디렉토리 소스, `requirements.txt` | Python 3.10 slim 기반 gunicorn 이미지 |
| `requirements.txt` | Python 런타임 의존성 목록이다. | pip | Flask, Kubernetes client, PyMySQL, Redis, requests, flasgger, gunicorn 설치 |
| `Makefile` | Helm 배포 shortcut을 둔 파일이다. | `make deploy`, Helm chart 경로 | config-server Helm upgrade/install 실행 |
| `base_etc/` | NFS 계정 파일이 비어 있을 때 seed로 쓰는 기본 passwd/group/shadow/bash 파일이다. | 기본 Linux 계정 템플릿 | `/kube_share` 하위 계정 파일 초기값 |
| `Chart/` | config-server 배포용 Helm chart이다. | Helm values | Deployment, Service, RBAC, ServiceAccount 리소스 |

## `main.py` API와 함수

| 이름 | 종류 | 역할 | 입력 | 출력/효과 |
| --- | --- | --- | --- | --- |
| `health` | route `GET /health` | 서버 상태 확인 | 없음 | `"OK"`, HTTP 200 |
| `load_k8s` | function | in-cluster config를 우선 로드하고 실패 시 kubeconfig를 로드한다. | 없음 | Kubernetes client 설정 |
| `reconcile_nodeport_allocations` | function | MySQL의 `nodeport_allocations`와 실제 Kubernetes NodePort Service 상태를 동기화한다. | namespace | 삭제한 stale DB row 수 |
| `allocate_nodeports` | function | 요청된 내부 포트마다 사용 가능한 NodePort를 DB row lock으로 할당한다. | username, pod_name, node_name, port dict list | `internal_port`, `external_port`, `usage_purpose` 목록 |
| `release_nodeports` | function | 특정 Pod의 NodePort 할당 row를 삭제한다. | pod_name | DB row 삭제 |
| `create_pod` | route `POST /create-pod` | WAS 사용자 정보를 조회하고 최적 GPU 노드를 선택해 Pod와 NodePort Service를 생성한다. | JSON `{"username": ...}` | 201 JSON `{status,node,pod_name,ports}` 또는 오류 |
| `_normalize_gid_list` | function | 단일 gid 또는 gid 목록을 int 목록으로 정규화한다. | raw gid 값 | `List[int]` |
| `_resolve_primary_group` | function | passwd/group 파일에서 사용자의 primary gid와 group name을 찾는다. | username, gid list | `(primary_gid, primary_group_name)` |
| `_get_sudo_allowed_commands` | function | 앱 설정의 sudo 허용 명령 목록을 가져온다. | 없음 | command string list |
| `_build_sudoers_policy` | function | 사용자별 sudoers 정책 라인을 생성한다. | username | 정책 문자열 또는 `None` |
| `_get_account_file_subpaths` | function | Pod에 mount할 계정 파일 subPath 목록을 만든다. | 없음 | subPath 문자열 목록 |
| `build_pod_spec` | function | ContainerSSH가 생성할 Kubernetes Pod spec과 NodePort 할당 결과를 만든다. | username, user_info, target_node, pod_name | ContainerSSH config wrapper dict, allocated ports |
| `delete_pod` | route `POST /delete-pod` | Pod, NodePort Service, NodePort DB row를 정리한다. | JSON `{"pod_name": ...}` | JSON `{status:"deleted"}` |
| `_migrate_internal` | function | 현재 Pod와 후보 노드 GPU 점수를 비교하고 더 좋은 노드로 이동한다. | request data dict | Flask JSON response |
| `migrate` | route `POST /migrate` | 사용자 Pod GPU 노드 마이그레이션을 lock으로 감싸 실행한다. | JSON `{"username":..., "nodes":[...], "min_improvement_ratio":...}` | migrated/skipped/error JSON |
| `create_or_resize_pvc` | route `POST /pvc` | 사용자/그룹 PVC를 생성하거나 기존 PVC 용량을 확장한다. | JSON `{"pvcs":[{"name","type","storage","pvc_name?"}]}` 또는 legacy username/storage | JSON `{results:[...]}` |
| `delete_pvc` | route `DELETE /pvc` | PVC와 연결 NFS 디렉토리를 삭제한다. | JSON `{"pvcs":[{"name","type","pvc_name?"}]}` 또는 legacy username/type | JSON `{results:[...]}` |
| `list_users` | route `GET /accounts/users` | passwd 파일의 사용자 목록을 반환한다. | 없음 | JSON `{users:[...]}` |
| `get_user` | route `GET /accounts/users/<username>` | 사용자 상세와 primary/supplementary group 정보를 반환한다. | path username | JSON `{user,groups}` |
| `create_user` | route `PUT /accounts/users` | passwd/group/shadow/sudoers 파일에 사용자를 추가한다. | JSON `name`, `uid`, `gid`, `passwd_sha512`, 선택 필드 | 201 JSON `{status,user,group,sudoers}` |
| `delete_user` | route `DELETE /accounts/users/<username>` | 사용자와 shadow/sudoers/member group 정보를 삭제한다. | path username | JSON `{status,user}` |
| `delete_group` | route `DELETE /accounts/groups/<groupname>` | primary group으로 쓰이지 않는 그룹을 삭제한다. | path groupname | JSON `{status,group,gid}` |
| `add_group` | route `PUT /accounts/groups` | 새 Linux group row를 추가한다. | JSON `name`, `gid`, optional `members` | 201 JSON `{status,group}` |
| `add_user_groups` | route `PUT /accounts/users/<username>/groups` | 사용자를 보조 그룹에 추가한다. | path username, JSON `groups` | JSON `{status,user,groups}` |

## `utils.py` 클래스와 함수

| 이름 | 종류 | 역할 | 입력 | 출력/효과 |
| --- | --- | --- | --- | --- |
| `LockedFile` | class | NFS 파일을 조작할 때 `/tmp` lock 파일로 shared/exclusive lock을 잡는 context manager이다. | path, mode | open file object, 종료 시 unlock |
| `get_db_connection` | function | PyMySQL connection을 생성한다. | `DB_HOST`, `DB_USER`, `DB_PASSWORD`, `DB_NAME` | transaction mode DB connection |
| `load_k8s`, `resolve_k8s_node_name`, `is_pod_ready`, `get_existing_pod`, `generate_pod_name`, `delete_pod_util` | function group | Kubernetes 설정 로드, 노드명 정규화, Pod readiness/존재 확인, Pod 이름 생성/삭제를 수행한다. | namespace, username, pod object/name, node candidate | 정규화된 노드명, Pod명, bool, Kubernetes API 변경 |
| `create_nodeport_services`, `delete_nodeport_services` | function | 사용자 Pod별 NodePort Service를 생성/삭제한다. | username, namespace, pod_name, port mapping list | Kubernetes Service 생성/삭제 |
| `load_user_image`, `commit_and_save_user_image` | function | 저장된 사용자 tar 이미지를 로드하거나 Pod 내부 `save_image.sh`를 실행해 이미지를 저장한다. | username, base image, pod_name, namespace | 사용할 image name, Redis metadata, tar 이미지 저장 |
| `_local_lockfile_path` | function | NFS 경로에 대응하는 로컬 lock 파일 경로를 만든다. | NFS path | `/tmp/cssh_lock...` path |
| `ensure_dir`, `ensure_file`, `ensure_seeded_file`, `ensure_etc_layout`, `ensure_sudoers_dir` | function group | 계정 파일 디렉토리와 seed 파일을 준비한다. | path, template name | 디렉토리/파일 생성 또는 초기 내용 복사 |
| `read_passwd_lines`, `write_passwd_lines`, `parse_passwd_line`, `format_passwd_entry` | function group | passwd 파일을 읽고 쓰며 행과 dict를 상호 변환한다. | passwd lines 또는 entry dict | passwd line list 또는 formatted line |
| `read_group_lines`, `write_group_lines`, `parse_group_line`, `format_group_entry` | function group | group 파일을 읽고 쓰며 멤버 목록을 dict로 변환한다. | group lines 또는 entry dict | group line list 또는 formatted line |
| `read_shadow_lines`, `write_shadow_lines`, `parse_shadow_line`, `format_shadow_entry` | function group | shadow 파일을 읽고 쓰며 패스워드 aging 필드를 변환한다. | shadow lines 또는 entry dict | shadow line list 또는 formatted line |
| `create_directory_with_permissions`, `delete_directory_if_exists` | function | NFS PVC 디렉토리를 만들고 uid/gid 권한을 맞추거나 삭제한다. | username/PV name, pvc_type, optional username | NFS 디렉토리 생성/권한 변경/삭제 |
| `get_node_gpu_score`, `select_best_node_from_prometheus` | function | Prometheus query로 GPU 노드 부하 점수를 계산하고 최적 노드를 고른다. | node list, Prometheus URL, timeout | score float 또는 best node |
| `get_group_members_home_volumes` | function | 같은 그룹 구성원의 홈 PVC를 읽기 전용 mount로 추가하기 위한 spec 조각을 만든다. | gid list, current username | `(volume_mounts, volumes)` |

## `bg_img_redis.py` 함수

| 함수 | 역할 | 입력 | 출력/효과 |
| --- | --- | --- | --- |
| `save_image_metadata` | 사용자 이미지 상태를 Redis에 저장한다. | username, status, size_mb, version, path | 저장된 dict |
| `get_image_metadata` | 특정 사용자 이미지 metadata를 조회한다. | username | dict 또는 `None` |
| `get_all_images` | `img:*` key 전체를 사용자명 기준 dict로 반환한다. | 없음 | `{username: metadata}` |
| `delete_image_metadata` | 특정 사용자 이미지 metadata를 삭제한다. | username | Redis key 삭제 |

## `test.py` 함수

| 함수 | 역할 | 입력 | 출력/효과 |
| --- | --- | --- | --- |
| `health` | mock 서버 헬스체크이다. | 없음 | `"OK"` |
| `config` | mock 사용자 정보로 ContainerSSH Kubernetes Pod config를 반환한다. | JSON `username` | ContainerSSH config JSON |
| `report_background` | 세션 종료 후 background process 여부를 확인해 Pod 삭제 여부를 결정한다. | JSON `username`, `pod_name`, optional `has_background` | background/deleted JSON |
| `create_or_resize_pvc`, `resize_pvc` | 단일 사용자 PVC를 생성/확장한다. | JSON `username`, `storage` | created/resized JSON |
| `select_best_node_from_prometheus` | 하드코딩된 Prometheus URL로 최적 노드를 고르는 실험 함수이다. | node list | best node 또는 `None` |
| `create_user`, `delete_user`, `add_user_groups` | 레거시 `/accounts` 계정 CRUD API이다. | JSON 또는 path username | passwd/group/shadow 파일 갱신 JSON |

## `main.py` 주요 함수 동작 상세

아래 함수들은 config-server의 핵심 실행 경로이다. 위의 표는 그대로 함수 목록을 빠르게 보는 용도이고, 이 섹션은 실제로 어떤 순서로 동작하는지 이해하기 위한 설명이다.

### `create_pod`

`POST /create-pod` 요청을 받아 사용자 작업용 Kubernetes Pod를 실제로 생성하는 가장 중요한 API이다. 입력은 JSON body의 `username`이고, 정상 처리되면 새 Pod 이름, 배치된 노드, 할당된 NodePort 목록을 반환한다.

동작 순서는 다음과 같다.

1. 요청 body에서 `username`을 읽고 없으면 400을 반환한다.
2. `WAS_URL_TEMPLATE`에 username을 넣어 외부 WAS에서 사용자 설정을 조회한다. 여기에는 사용할 이미지, UID/GID, 접근 가능한 GPU 노드 목록, 자원 제한, 추가 포트 등이 들어온다고 가정한다.
3. `generate_pod_name()`으로 `containerssh-<username>-<random>` 형식의 Pod 이름을 만든다.
4. Kubernetes API로 같은 이름의 Pod가 이미 있는지 확인한다. 충돌하면 409를 반환한다.
5. WAS에서 받은 `gpu_nodes`를 후보 노드 목록으로 만들고, `select_best_node_from_prometheus()`로 GPU 사용량 점수가 가장 낮은 노드를 고른다.
6. `build_pod_spec()`를 호출해 Kubernetes Pod spec과 NodePort 할당 결과를 만든다. 이 단계 안에서 계정 파일 준비, 이미지 선택, PVC mount, GPU device mount, 그룹 공유 홈 mount, NodePort DB 할당이 함께 처리된다.
7. Kubernetes에 Pod를 생성하고 최대 60초 동안 Ready 상태를 기다린다.
8. Pod가 Ready가 되면 `create_nodeport_services()`로 SSH/Jupyter/추가 포트용 NodePort Service를 생성한다.
9. 성공하면 `{status, node, pod_name, ports}`를 201로 반환한다.

실패 처리도 중요하다. Pod 생성, Ready 대기, Service 생성 중 문제가 생기면 `release_nodeports()`로 DB에 잡아둔 포트를 해제하고, 생성된 Pod가 있으면 삭제를 시도한다. 즉, `create_pod()`는 Pod와 NodePort DB 상태가 어긋나지 않도록 rollback 성격의 정리를 포함한다.

### `build_pod_spec`

`create_pod()`와 `migrate()`가 공통으로 사용하는 Pod spec 생성 함수이다. 입력은 username, WAS에서 받은 `user_info`, target node, pod name이다. 출력은 ContainerSSH가 이해하는 config wrapper와 실제 할당된 port 목록이다.

주요 처리 흐름은 다음과 같다.

1. `ensure_etc_layout()`로 `/kube_share` 계정 파일 구조를 준비한다. 비어 있는 passwd/group/shadow/bash 파일은 `base_etc/` 템플릿으로 채운다.
2. `resolve_k8s_node_name()`으로 target node가 실제 cluster node와 매칭되는지 확인하고 소문자 기준 이름으로 정규화한다.
3. `load_user_image()`로 `/image-store/images/user-<username>.tar`가 있으면 사용자 저장 이미지를 로드하고, 없거나 실패하면 WAS가 준 base image를 사용한다.
4. passwd/group 파일을 읽어 사용자의 primary gid와 group name을 결정한다.
5. 기본 포트 22(ssh), 8888(jupyter)에 WAS의 `additional_ports`를 더한 뒤 `allocate_nodeports()`로 외부 NodePort를 선점한다.
6. 선택된 GPU 노드 정보에서 CPU, memory, GPU 개수를 읽고 resource limit과 GPU device hostPath mount를 구성한다.
7. 사용자 홈 PVC, image-store PVC, 같은 그룹 구성원 홈 PVC, 계정 파일(passwd/group/shadow/bashrc/bash_logout/sudoers)을 volume과 volumeMount로 추가한다.
8. 최종 Pod metadata, container env, resource, volume spec을 dict로 만들어 반환한다.

이 함수에서 NodePort 할당이 이미 일어나므로, spec 생성 후 예외가 발생하면 `release_nodeports()`를 호출해 DB allocation을 되돌린다. 따라서 이 함수는 단순 dict builder가 아니라 "Pod 생성 전에 필요한 외부 상태를 일부 선점하는 함수"로 이해하는 편이 정확하다.

### `allocate_nodeports`

사용자 Pod 내부 포트와 외부 NodePort를 연결하기 위해 MySQL `nodeport_allocations` table에 포트 점유 정보를 저장한다. 입력은 username, pod name, node name, 내부 포트 목록이다.

처음에는 `reconcile_nodeport_allocations()`를 호출해 MySQL에는 남아 있지만 Kubernetes에는 Service가 없는 stale allocation을 정리한다. 이 reconcile은 5분 throttle이 걸려 있어 매 요청마다 Kubernetes와 DB를 과하게 스캔하지 않는다.

그 다음 MySQL transaction에서 `SELECT node_port FROM nodeport_allocations FOR UPDATE`를 실행해 현재 사용 중인 포트를 row lock으로 잡는다. 사용 가능한 범위는 30000부터 32767까지이고, 요청 포트 수만큼 비어 있는 포트를 골라 insert한다. 모든 insert가 끝나면 commit하고, 실패하면 rollback한다.

이 함수의 반환값은 다음 형태의 list이다.

```json
[
  {"internal_port": 22, "external_port": 30001, "usage_purpose": "ssh"},
  {"internal_port": 8888, "external_port": 30002, "usage_purpose": "jupyter"}
]
```

### `delete_pod`

`POST /delete-pod` 요청을 받아 사용자 Pod와 연결 리소스를 정리한다. 입력은 JSON body의 `pod_name`이다.

처리 순서는 NodePort Service 삭제, NodePort DB allocation 해제, Kubernetes Pod 삭제이다. Pod 이름은 `containerssh-`로 시작해야 하며, 이름 형식이 맞지 않으면 400을 반환한다. Pod 이름에서 username을 파싱하지만, 현재 삭제 동작의 핵심 key는 username이 아니라 pod_name이다.

이 함수는 `create_pod()`의 반대 방향 정리 함수이다. 운영 중 수동 삭제가 필요할 때는 Pod만 직접 삭제하기보다 이 API를 통해 Service와 DB allocation까지 같이 정리하는 것이 안전하다.

### `migrate`와 `_migrate_internal`

`POST /migrate`는 실행 중인 사용자 Pod를 더 나은 GPU 노드로 옮기는 API이다. `migrate()` 자체는 username 기준 lock 파일(`/tmp/migrate-<username>.lock`)을 잡아 같은 사용자의 migration이 동시에 실행되지 않게 하고, 실제 로직은 `_migrate_internal()`이 처리한다.

`_migrate_internal()`은 먼저 현재 실행 중인 Pod를 찾고, 요청으로 받은 후보 노드 목록을 실제 Kubernetes node 이름으로 정규화한다. 현재 노드가 후보 목록에 없으면 잘못된 요청으로 보고, 후보가 현재 노드뿐이면 skip한다.

그 다음 Prometheus GPU score를 현재 노드와 다른 후보 노드들에 대해 계산한다. 가장 좋은 후보 노드의 점수가 현재 노드보다 `min_improvement_ratio`만큼 충분히 좋아야 migration을 진행한다. 개선 폭이 부족하면 Pod를 건드리지 않고 skip 응답을 반환한다.

실제 migration이 진행되면 기존 Pod 안에서 `commit_and_save_user_image()`를 실행해 사용자 상태를 image-store에 저장하고, 새 Pod 이름을 만든 뒤 `build_pod_spec()`와 Kubernetes API로 새 Pod를 생성한다. 새 Pod가 Ready가 되고 NodePort Service 생성까지 성공하면 기존 Pod의 Service, DB allocation, Pod를 삭제한다. 새 Pod 생성이나 Service 생성이 실패하면 새 Pod와 새 NodePort allocation을 정리하고 오류를 반환한다.

### `create_or_resize_pvc`

`POST /pvc`는 사용자 또는 그룹 PVC를 생성하거나 기존 PVC 용량을 확장한다. 표준 입력은 `pvcs` 배열이고, 예전 호출 방식인 `username`/`storage`도 user PVC 요청으로 변환해 처리한다.

각 PVC 요청마다 `name`, `type`, `storage`, optional `pvc_name`을 읽는다. `type`은 `user` 또는 `group`만 허용한다. PVC 이름은 직접 받은 `pvc_name`이 있으면 그것을 쓰고, 없으면 user는 `pvc-<name>-share`, group은 `pvc-<name>-group-share`로 만든다.

이미 PVC가 있으면 Kubernetes patch API로 storage request를 수정해 resize한다. 없으면 새 PVC를 만들고 최대 30초 동안 Bound 상태와 PV 이름을 기다린다. PV 이름을 얻으면 `create_directory_with_permissions()`에 PV 이름과 원래 사용자/그룹 이름을 넘겨 NFS 실제 디렉토리 소유권과 권한을 맞춘다. 여러 PVC를 한 요청에서 처리하므로 응답은 항상 `results` 배열 중심이다.

### `create_user`

`PUT /accounts/users`는 Pod 안에 mount될 Linux 계정 파일을 갱신하는 API이다. 실제 OS의 `/etc/passwd`를 직접 수정하는 것이 아니라, config-server가 관리하는 NFS 계정 파일(`/kube_share/passwd`, `/kube_share/group`, `/kube_share/shadow`, 선택적으로 `/kube_share/sudoers.d/<user>`)을 수정한다.

필수 입력은 `name`, `uid`, `gid`, `passwd_sha512`이다. 먼저 passwd에 같은 사용자가 있는지 확인하고, 없으면 passwd entry를 추가한다. 그 다음 primary group name과 gid를 기준으로 group entry가 없으면 새로 만든다. shadow에는 전달받은 SHA-512 crypt 패스워드와 password aging 기본값을 넣는다.

`SUDO_ALLOWED_COMMANDS` 설정이 있으면 `_build_sudoers_policy()`가 password-protected sudo whitelist 정책을 만들고, 사용자별 sudoers 파일을 `0440` 권한으로 생성한다. 이 API로 만든 계정 정보는 이후 `build_pod_spec()`에서 Pod에 read-only subPath mount되어 컨테이너 내부의 `/etc/passwd`, `/etc/group`, `/etc/shadow`처럼 보이게 된다.
