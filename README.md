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
- Docker Hub: DOCKER_USERNAME, DOCKER_PASSWORD
- Kubernetes Access: K8S_HOST, K8S_USERNAME, K8S_PRIVATE_KEY, K8S_PORT
