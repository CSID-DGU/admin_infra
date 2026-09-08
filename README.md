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

# 6. 후속 구현 필요 사항

## 유저 컨테이너 재시작/마이그레이션 시 패키지(홈 밖 설치분) 보존 (보류)

셀프 서비스 컨테이너 재시작(admin_be #481, admin_infra #152, admin_fe #124, 전부 미머지·보류)을 준비하다가, 관리자 마이그레이션 기능도 포함해서 **홈 디렉토리 밖에 설치한 패키지가 실제로는 전혀 보존되고 있지 않다는 걸 발견함.**

**원인**: `commit_and_save_user_image()`가 pod 안에서 `/usr/local/bin/save_image.sh`를 실행하려 하는데 이 스크립트가 base 이미지 어디에도 없음(`kubectl exec`로 실측 확인). 게다가 pod에는 `/var/run/docker.sock`이 마운트돼 있지 않아 컨테이너가 애초에 자기 자신을 커밋할 방법이 없음(보안상 마운트해서도 안 됨). `load_user_image()`는 저장된 이미지가 없으면 조용히 base 이미지로 폴백하기 때문에 에러 없이 매번 "그냥 초기화"되고 있었음.

**레거시 시스템과의 차이(중요)**: 레거시(uidctl, 순수 Docker)에서는 "같은 서버에서 재시작할 땐 패키지가 보존됐다"고 하는데, 이건 커스텀 로직이 아니라 **순정 `docker restart`**(같은 컨테이너를 멈췄다 다시 시작 — 레이어가 그대로 남음)였을 것으로 추정됨(uidctl 저장소 전체에 migrate/commit 관련 코드가 전혀 없음, 다른 서버로의 이전 자체는 레거시에서도 불가능했다고 확인됨 - 임준영). **k8s/containerd는 이 동작이 다름** — pod 재시작(`crictl stop` 등)은 컨테이너를 삭제 후 재생성하는 방식이라 레이어가 안 남는다는 걸 2026-09-07 FARM6 실측으로 확인함(재시작 전/후 컨테이너 ID 변경, 마커 파일 소실).

**검토했던 해법과 각각의 문제**:
1. **Docker Hub를 레지스트리로 재사용** — 이미 쓰고 있어서 새 인프라 불필요하지만, (a) 6시간당 pull rate limit 있음, (b) public으로 구워지면 민감 정보 노출 위험, private repo는 유료 seat 제한. 소규모 베타 검증엔 쓸 수 있어도 실제 운영 단계엔 부적합.
2. **사설 컨테이너 레지스트리(registry:2) + 노드별 커밋 전용 DaemonSet** — 정공법. 레이어 단위 증분 전송이라 속도/트래픽 문제 없고 프라이버시도 내부에 머무름. 다만 신규 인프라(레지스트리 배포, DaemonSet 신규 개발, 노드별 containerd mirror 설정 롤아웃)가 필요해 규모가 있음 — 별도 작업 세션 단위.
   - **범위 축소 여지**: 재시작(reboot)은 같은 노드에 새 pod를 만드는 것으로 이미 제한했으므로, 커밋한 이미지가 그 노드의 로컬 docker/containerd에 그대로 남아 `imagePullPolicy: IfNotPresent`로 바로 재사용된다 — **레지스트리 push/pull 자체가 불필요**하다. 레지스트리는 마이그레이션(다른 노드로 이동)에만 필요하므로, 재시작만 우선 지원한다면 DaemonSet만 만들면 되고 레지스트리·containerd mirror 설정 롤아웃은 생략 가능 — 훨씬 작은 작업으로 축소됨.
3. **심볼릭 링크로 영구 마운트 범위 확장** (Kubeflow/GitHub Codespaces가 실제로 쓰는 방식 — 업계 표준에 가까움, 2026-09-08 웹 검색으로 확인): entrypoint.sh에서 `/opt`, `/usr/local`을 홈 하위 디렉토리로 심볼릭 링크. k8s 설정 변경 없이 이미지 스크립트 수정만으로 가능해 셋 중 구현이 가장 쉬움.
   - **커버됨**: `conda install`(기본 위치 `/opt/conda`), `./configure && make install` 류 소스 빌드 설치(`/usr/local`), 기타 `/opt` 설치 툴. 참고로 `pip install --user`, Hugging Face/PyTorch 캐시(`~/.cache`), cargo/go/R 패키지는 애초에 `$HOME` 기준이라 **이미 지금도 보존됨** (`HOME=/home/<username>`로 세팅되어 있음).
   - **안 됨, 그리고 왜**: `apt install` — 설치 파일이 `/usr`, `/etc`, `/var/lib/dpkg` 등 여러 경로에 흩어짐. 이 중 `/usr`만 영구화해도 안 되는데, 매 명령 실행마다 공유 라이브러리를 읽는 경로라 NFS에 올리면 평상시 성능이 떨어지고, 베이스 이미지를 업데이트해도(CUDA 버전업, 보안 패치) 유저의 `/usr`는 그 시점 스냅샷에 영구 고정되어버려 이미지 버전 관리 체계가 무력화됨. `/var/lib/dpkg`(설치 이력 DB)만 따로 영구화하는 것도 의미 없음 — 이 DB는 실제 `/usr` 내용과 항상 일치해야 하는데, `/usr`는 매번 초기화되니 "설치됐다고 기록은 있는데 파일은 없는" 불일치가 발생해 apt 자체가 깨짐. `/etc` 전체 심볼릭 링크도 위험 — entrypoint.sh가 매 시작마다 새로 세팅하는 `/etc/passwd`, `/etc/ssh/sshd_config` 등과 이전 실행의 잔여 상태가 충돌해 로그인/SSH 자체가 안 될 수 있음. `sources.list.d`/`cron.d`/`sudoers.d` 같은 개별 파일 단위 심볼릭 링크는 기술적으로는 가능하나 얻는 이득 대비 리스크가 커서 우선순위 낮음.
   - **파일시스템과 무관하게 절대 안 되는 것**: 실행 중이던 프로세스(학습 job, `nohup` 백그라운드, tmux 세션), Jupyter 커널의 저장 안 한 메모리 상태, 열려있던 네트워크 연결. 재시작 = 프로세스 종료이므로 어떤 방식으로도 못 지킴(학습 중이던 job을 지키려면 재시작을 안 하거나, 학습 스크립트 자체가 체크포인트를 남기게 하는 완전히 별개의 문제).

**결정**: 2026-09-07 기준 전부 보류. 재개 시: 3번(심볼릭 링크)을 먼저 적용해 conda/`/usr/local`/`/opt` 보존부터 빠르게 확보하고, apt까지 필요하면 2번을 같은 노드 한정(레지스트리 없이 DaemonSet만)으로 축소해 진행하는 순서를 권장.

<br>

# 7. 환경 변수 및 시크릿
CI/CD 작동을 위해 GitHub Repository Secrets에 다음 변수들이 등록되어 있습니다.
- Docker Hub: `DOCKER_USERNAME`, `DOCKER_PASSWORD`
- Kubernetes Access: `K8S_HOST`, `K8S_USERNAME`, `K8S_PRIVATE_KEY`, `K8S_PORT`
> 현재는 username이 toni와 key로 되어있으며, 관리자 변경 시 인수인계가 필요합니다.
