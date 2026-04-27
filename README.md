# 🚀 GPU 서버 관리 자동화 시스템 Infra Server 배포 및 운영 가이드

이 문서는 `config-server`의 Git 브랜치 전략, CI/CD 파이프라인 구조, 그리고 배포 절차를 정의합니다.
> (나머지 자동화는 진행중 👻)

## 1. 브랜치 전략 (Branch Strategy)

우리는 **Git Flow** 전략을 기반으로 운영하며, `main` 브랜치에 코드가 통합될 때만 실제 서버 배포가 이루어집니다.

| 브랜치 이름 | 역할 | 배포 여부 | 비고 |
| :--- | :--- | :---: | :--- |
| **`main`** | **운영(Production) 환경** | **O (자동)** | 배포 시점: PR Merge 직후 |
| **`develop`** | **개발(Development) 통합** | X | 기능 개발 후 통합 테스트 용도 |
| `feature/*` | 개별 기능 개발 | X | `develop`에서 분기하여 작업 |
| `hotfix/*` | 운영 이슈 긴급 수정 | O | `main`에서 분기, Merge 후 즉시 배포 (사용 권장 X)|

---

## 2. CI/CD 파이프라인 (Deployment Pipeline)

배포 자동화는 **GitHub Actions**를 사용하며, 오직 `main` 브랜치에 `push` 이벤트가 발생할 때 실행됩니다.

### 🔄 배포 흐름 (Workflow)
1.  **Trigger**: `develop` → `main`으로 PR이 Merge 되면 워크플로우가 시작됩니다.
2.  **Build & Push**:
    * 소스 코드를 기반으로 Docker 이미지를 빌드합니다.
    * 이미지 태그는 `latest`와 `Git Commit Hash` 두 가지로 생성됩니다.
    * Docker Hub의 팀/조직 레포지토리로 Push 됩니다.
3.  **Deploy (Helm Upgrade)**:
    * GitHub Actions가 운영 서버(`farm8`)에 SSH로 접속합니다.
    * `helm upgrade` 명령어를 통해 Kubernetes 배포를 수행합니다.
    * **Key Config**: `--set image.pullPolicy=Always` 옵션을 통해 항상 최신 이미지를 다운로드 받도록 강제합니다.

---

## 3. 작업 및 배포 규칙 (Workflow Rules)

팀원 간 충돌을 방지하고 안정적인 배포를 위해 아래 절차를 준수해 주세요.

### 🛠 기능 개발 (Feature)
1.  본인이 생성한 Github 이슈 번호에 맞춰 `develop` 브랜치에서 `feature/#기능번호-기능명` 브랜치를 생성합니다. (e.g. feat/#155-scheduler)
3.  로컬에서 개발 및 테스트를 진행합니다.
4.  커밋 메시지 양식: [분류] #issue 설명 (e.g. `[feat] #4 메인 기능 만들기`)
6.  작업이 완료되면 `feature` → `develop` 브랜치로 Pull Request(PR)를 생성합니다.

### 🚀 정기 배포 (Release)
1.  `develop` 브랜치에 충분한 기능이 모이고 테스트가 완료되면 배포를 준비합니다.
2.  PR 제목: `[deploy] develop -> main (또는 부가 설명)`  **`develop` → `main`** 으로 PR을 생성합니다. 
3.  코드 리뷰(Approve) 후 Merge 버튼을 누르면, **즉시 운영 서버에 배포됩니다.** 최소 한 명 이상의 Approve를 받아야 합니다.

---

## 4. API 문서 및 모니터링

서버가 정상적으로 실행 중일 때, 아래 주소에서 API 명세(Swagger)를 확인할 수 있습니다.

* **Swagger UI**: `http://{farm_server_ip}:9732/apidocs/`
* **Health Check**: `http://{farm_server_ip}:9732/health`

> **참고**: NodePort는 `values.yaml` 설정에 따라 **9732**번 포트를 사용합니다.

---

## 5. 트러블슈팅 (Troubleshooting)

배포 후 문제가 발생했을 때 확인 및 조치 방법입니다.

### 1. Pod 상태 확인
```bash
kubectl get pods -n cssh
```
- 정상: Running (READY 1/1)
- 오류: CrashLoopBackOff, ImagePullBackOff, Pending

### 2. 로그 확인 

서버가 뜨지 않거나 동작이 이상할 때 실시간 로그를 확인합니다.
```bash
# Pod 이름 확인 후
kubectl logs -f <POD_NAME> -n cssh
```
주요 체크 포인트:

- ModuleNotFoundError: requirements.txt 누락 또는 파일명 불일치
- WORKER TIMEOUT: 초기 로딩 시간이 긺 (Dockerfile 타임아웃 설정 확인)

### 3. 배포된 이미지 버전 확인
제대로 된 버전이 배포되었는지 커밋 해시를 통해 확인합니다.

```bash
kubectl describe pod <POD_NAME> -n cssh | grep Image
```
이미지 태그가 `v10` 같은 고정 값이 아니라, `난수(Commit Hash)`로 되어 있어야 정상 배포된 것입니다.

# 6. 환경 변수 및 시크릿
CI/CD 작동을 위해 GitHub Repository Secrets에 다음 변수들이 등록되어 있습니다.
- Docker Hub: `DOCKER_USERNAME`, `DOCKER_PASSWORD`
- Kubernetes Access: `K8S_HOST`, `K8S_USERNAME`, `K8S_PRIVATE_KEY`, `K8S_PORT`
> 현재는 username이 toni와 key로 되어있으며, 관리자 변경 시 인수인계가 필요합니다.

---

## 7. 루트 디렉토리 파일 구성

이 저장소는 ContainerSSH 기반 GPU 작업 환경을 구성하기 위한 인증 서버, 설정 서버, 게스트 이미지, Helm 차트, 운영 스크립트를 함께 관리합니다.

| 파일 | 역할 | 주요 입력 | 주요 출력/효과 |
| --- | --- | --- | --- |
| `README.md` | 저장소 운영 가이드와 전체 구성 설명을 제공합니다. | 운영자, 개발자 문서 요구사항 | 배포/운영 절차 문서 |
| `restart_auth.sh` | 인증 서버 이미지를 다시 빌드하고 Kubernetes 리소스를 적용한 뒤 Deployment를 재시작합니다. | 로컬 `auth-server` 소스, `kubectl` 컨텍스트 | `containerssh-auth-server:latest` 이미지, 인증 서버 롤아웃 |
| `restart_containerssh.sh` | ContainerSSH Helm 릴리스를 업그레이드하고 Deployment를 재시작합니다. | `containerssh` Helm 차트, `kubectl`/`helm` 컨텍스트 | `containerssh` 릴리스 갱신, Pod 상태 출력 |
| `restart_pvc.sh` | 사용자 PVC Helm 차트를 업그레이드하고 PV/PVC 상태를 확인합니다. | `pvc-chart` Helm 차트 | PVC 리소스 갱신, PV/PVC 목록 출력 |
| `k8s_account_manager.py` | `/etc/passwd`, `/etc/group`, `/etc/shadow`, `/etc/sudoers.d` 파일을 직접 CRUD하는 독립형 Python 유틸리티입니다. | 계정 파일 경로, 사용자/그룹/패스워드/권한 정책 데이터 | 갱신된 Linux 계정 파일과 sudoers 파일 |
| `bashrc` | 게스트 컨테이너 사용자 셸에 주입되는 `.bashrc` 템플릿입니다. 로그아웃 감지, 세션 모니터링, 진단 alias를 제공합니다. | 셸 환경 변수, SSH 세션 상태 | `$HOME/.kube_logs/*`, logout hook 실행 |
| `bash.bash_logout` | 게스트 컨테이너 로그아웃 시 config-server의 `/report-background`로 세션 종료 상태를 보고합니다. | `USER`, `hostname`, config-server HTTP endpoint | `$HOME/.kube_logs/logout.log`, background report API 호출 |
| `.gitignore` | Git 추적 제외 규칙입니다. | Git working tree | 제외 파일 미추적 |
| `.github/` | GitHub Actions와 이슈 템플릿을 보관합니다. | GitHub 이벤트, 이슈 작성 내용 | CI/CD 실행, 표준화된 이슈 생성 |

### `k8s_account_manager.py` 클래스와 함수

| 이름 | 종류 | 역할 | 입력 | 출력/효과 |
| --- | --- | --- | --- | --- |
| `AccountDB` | class | passwd/group/shadow/sudoers 파일 조작을 하나의 고수준 API로 묶습니다. | 계정 파일 경로 4개 | `create_user`, `delete_user`, `set_password`, `lock`, `unlock`, 그룹/권한 메서드 제공 |
| `_atomic_write` | function | 임시 파일에 쓴 뒤 `os.replace`로 원자적 저장을 수행합니다. | `path`, `data`, `mode`, 선택적 `uid/gid` | 대상 파일 갱신 |
| `_today_days_since_epoch` | function | shadow 날짜 필드용 일수를 계산합니다. | 없음 | 1970-01-01 이후 일수 |
| `make_password_hash` | function | 평문 패스워드를 SHA-512 crypt 해시로 변환합니다. | `plaintext` | shadow에 넣을 해시 문자열 |
| `which` | function | PATH에서 실행 파일 위치를 찾습니다. | 명령 이름 | 절대 경로 또는 `None` |
| `parse_passwd_line`, `serialize_passwd_entry`, `load_passwd`, `save_passwd`, `upsert_passwd`, `delete_passwd_user` | function group | `/etc/passwd` 행을 dict로 변환하거나 파일 단위로 읽기/쓰기/추가/삭제합니다. | passwd 파일 경로, 사용자 entry dict 또는 username | passwd entry list 또는 갱신된 passwd 파일 |
| `parse_group_line`, `serialize_group_entry`, `load_group`, `save_group`, `upsert_group`, `delete_group`, `add_user_to_group`, `remove_user_from_group` | function group | `/etc/group` 행을 파싱하고 그룹/멤버십을 관리합니다. | group 파일 경로, group entry, group/user 이름 | group entry list 또는 갱신된 group 파일 |
| `parse_shadow_line`, `serialize_shadow_entry`, `load_shadow`, `save_shadow`, `upsert_shadow`, `delete_shadow_user`, `set_shadow_password`, `lock_shadow_account`, `unlock_shadow_account` | function group | `/etc/shadow` 패스워드 해시와 계정 잠금 상태를 관리합니다. | shadow 파일 경로, username, 평문/해시 패스워드 | shadow entry list 또는 갱신된 shadow 파일 |
| `write_sudoers_user`, `delete_sudoers_user` | function | `/etc/sudoers.d/<user>` 정책 파일을 생성/삭제합니다. | sudoers 디렉토리, username, 정책 라인 | sudoers 파일 생성/삭제, 선택적 `visudo` 검증 |

### 루트 Shell 스크립트 함수

| 파일 | 함수 | 역할 | 입력 | 출력/효과 |
| --- | --- | --- | --- | --- |
| `bashrc` | `run_logout_once` | 같은 SSH 세션에서 로그아웃 hook이 중복 실행되지 않도록 `/tmp/.logout_once_<sid>` lock을 잡습니다. | session id, `SESSION_ID`, `ORIGINAL_PID` | 최초 1회만 `$HOME/.bash_logout` 실행 |
| `bashrc` | `safe_logout` | blocking/non-blocking 모드에 따라 `run_logout_once`를 호출합니다. | original pid, `LOGOUT_BLOCKING_MODE` | logout hook 실행 또는 background 실행 |
| `bashrc` | `kst_date` | UTC 기준 시간을 KST 문자열로 변환합니다. | optional date format args | KST timestamp 문자열 |
| `bashrc` | `ssh_session_monitor` | interactive bash 세션의 PID/PPID 상태를 주기적으로 감시합니다. | 현재 bash PID, SSH 환경변수 | monitor log, 세션 종료 감지 시 logout hook |
| `bashrc` | `check_session` | prompt 실행 시 세션 활성 timestamp를 갱신합니다. | 현재 shell PID | `/tmp/session_active_<pid>` |
| `bash.bash_logout` | `on_farewell_custom` | 로그아웃 시 config-server `/report-background` endpoint로 사용자와 Pod 이름을 보고합니다. | `USER`, `hostname`, HTTP endpoint | logout log, HTTP POST 결과 |
