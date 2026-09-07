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
3. **(더 가벼운 대안, 미검토) PVC 경로 확장** — 임준영 제안: 홈처럼 특정 설치 경로(예: `/opt/conda`)도 PVC로 영구 마운트하면, 그 경로에 설치한 것만이라도 이미지 커밋 없이 재시작/마이그레이션에서 보존 가능. apt로 시스템 전역에 설치하는 건 여전히 못 지키지만(그 경로까지 영구화하면 컨테이너를 사실상 영구 디스크 가진 VM으로 만드는 셈이라 배보다 배꼽), conda/pip 유저 영역 위주라면 이 쪽이 registry+DaemonSet보다 훨씬 가볍게 상당 부분을 해결할 수 있어 재개 시 먼저 검토해볼 가치 있음.

**결정**: 2026-09-07 기준 전부 보류. 재개 시 위 3가지 옵션부터 비교 검토.

<br>

# 7. 환경 변수 및 시크릿
CI/CD 작동을 위해 GitHub Repository Secrets에 다음 변수들이 등록되어 있습니다.
- Docker Hub: `DOCKER_USERNAME`, `DOCKER_PASSWORD`
- Kubernetes Access: `K8S_HOST`, `K8S_USERNAME`, `K8S_PRIVATE_KEY`, `K8S_PORT`
> 현재는 username이 toni와 key로 되어있으며, 관리자 변경 시 인수인계가 필요합니다.
