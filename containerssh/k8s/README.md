# ContainerSSH Kubernetes Deployment

이 디렉토리는 ContainerSSH를 Kubernetes 클러스터에 배포하기 위한 설정 파일들을 포함합니다.

## 파일 구조

```
./containerssh/k8s/
├── namespace.yaml      # containerssh 네임스페이스
├── serviceaccount.yaml # ServiceAccount 설정
├── rbac.yaml          # Role & RoleBinding (RBAC 권한)
├── configmap.yaml     # ContainerSSH 설정
├── deployment.yaml    # ContainerSSH 애플리케이션 배포
├── service.yaml       # ContainerSSH 서비스 (NodePort)
├── Makefile          # 배포/관리 명령어
└── README.md         # 이 파일
```

## 사용법

### 배포

```bash
# ContainerSSH 배포
make deploy
```

### 상태 확인

```bash
# 배포 상태 확인
make status

# 로그 확인
make logs

# 실시간 로그 확인
make logs-follow
```

### SSH 연결 테스트

```bash
# SSH 연결 정보 확인
make ssh-test

# 실제 SSH 연결 (출력된 명령어 사용)
ssh -o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedKeyTypes=+ssh-rsa testuser@localhost -p [NodePort]
```

### 설정 업데이트

```bash
# ConfigMap 업데이트 후 재시작
make update-config

# 수동 재시작
make restart
```

### 정리

```bash
# 모든 ContainerSSH 리소스 삭제
make clean
```

## 주요 설정

### ContainerSSH 설정 (configmap.yaml)

- **Backend**: Kubernetes
- **Auth**: Webhook 방식 (containerssh-auth-service 사용)
- **Guest Image**: containerssh/containerssh-guest-image
- **Namespace**: containerssh (게스트 컨테이너도 같은 네임스페이스에 생성)

### 네트워크 설정

- **Service Type**: NodePort
- **SSH Port**: 2222
- **NodePort**: 자동 할당 (30000-32767 범위)

### RBAC 권한

ContainerSSH ServiceAccount는 다음 권한을 가집니다:
- Pod 생성, 조회, 삭제
- Pod 로그 조회
- Pod exec 실행

## 트러블슈팅

### SSH 연결 실패

1. **호스트 키 문제**: `-o HostKeyAlgorithms=+ssh-rsa -o PubkeyAcceptedKeyTypes=+ssh-rsa` 옵션 사용
2. **포트 확인**: `make status`로 정확한 NodePort 확인
3. **로그 확인**: `make logs`로 ContainerSSH 로그 확인

### Pod 시작 실패

1. **Secret 확인**: `kubectl get secret containerssh-hostkey -n containerssh`
2. **ConfigMap 확인**: `kubectl get configmap containerssh-config -n containerssh`
3. **RBAC 확인**: `kubectl get role,rolebinding -n containerssh`

### 게스트 컨테이너 생성 실패

1. **ServiceAccount 권한**: `kubectl describe role containerssh-role -n containerssh`
2. **이미지 풀링**: `containerssh/containerssh-guest-image` 이미지 접근 가능 여부 확인

## 도움말

```bash
# 사용 가능한 모든 명령어 확인
make help
```
