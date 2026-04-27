# ContainerSSH Authentication Server with MySQL

ContainerSSH를 위한 MySQL 기반 인증 서버입니다. 패스워드 인증, 공개키 인증, 그리고 **REST API를 통한 사용자 관리 기능**을 제공합니다.

## 🆕 새로운 기능

### REST API 사용자 관리
복잡한 Makefile 명령어 대신 **HTTP API**로 사용자를 관리할 수 있습니다!

- **웹 브라우저에서 관리**: Swagger UI (`http://localhost:8080/docs`)
- **API 호출로 관리**: curl 명령어로 간단하게
- **스크립트 자동화**: 쉬운 API 호출로 자동화 가능

## 주요 기능

- **패스워드 인증**: bcrypt 해싱을 사용한 안전한 패스워드 인증
- **공개키 인증**: SSH 공개키 기반 인증
- **🆕 사용자 관리 API**: REST API를 통한 사용자 CRUD 작업
- **🆕 공개키 관리 API**: 사용자별 SSH 공개키 관리
- **MySQL 백엔드**: 확장 가능한 데이터베이스 백엔드
- **Kubernetes 지원**: 완전한 Kubernetes 배포 설정
- **RESTful API**: FastAPI 기반의 REST API with Swagger 문서
- **헬스체크**: 애플리케이션 및 데이터베이스 상태 모니터링

## 빠른 시작

### 1. 전체 배포

```bash
# 전체 시스템 배포 (MySQL + 인증 서버)
make deploy

# 배포 상태 확인
make status

# MySQL이 준비될 때까지 대기
make wait-for-mysql

# API 접근을 위한 포트 포워딩
make port-forward
```

### 2. 웹 인터페이스로 관리

브라우저에서 `http://localhost:8080/docs`로 접속하면 **Swagger UI**에서 모든 API를 테스트할 수 있습니다!

### 3. API로 사용자 관리

```bash
# 🚀 빠른 사용자 추가
make add-user-api USER=test123 PASSWORD=test123

# 📋 사용자 목록 조회
make list-users-api

# 또는 직접 curl 사용
curl http://localhost:8080/users
```

## 🔧 사용자 관리 API

### 기본 사용법

```bash
# 포트 포워딩 시작 (한 번만 실행)
make port-forward
```

### 👤 사용자 관리

```bash
# 사용자 목록 조회
curl http://localhost:8080/users

# 새 사용자 추가
curl -X POST http://localhost:8080/users \
  -H "Content-Type: application/json" \
  -d '{"username":"newuser","password":"securepass123"}'

# 특정 사용자 조회
curl http://localhost:8080/users/newuser

# 사용자 삭제 (비활성화)
curl -X DELETE http://localhost:8080/users/newuser
```

### 🔑 공개키 관리

```bash
# 사용자의 공개키 목록 조회
curl http://localhost:8080/users/newuser/keys

# 새 공개키 추가
curl -X POST http://localhost:8080/users/newuser/keys \
  -H "Content-Type: application/json" \
  -d '{
    "public_key": "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC...",
    "key_name": "laptop-key"
  }'

# 공개키 삭제 (비활성화)
curl -X DELETE http://localhost:8080/users/newuser/keys/1
```

### 📊 API 문서

- **Swagger UI**: `http://localhost:8080/docs` - 대화형 API 문서
- **ReDoc**: `http://localhost:8080/redoc` - 깔끔한 API 문서

## API 엔드포인트 목록

### 🔐 인증 엔드포인트
- `POST /password` - 패스워드 인증 (ContainerSSH용)
- `POST /pubkey` - 공개키 인증 (ContainerSSH용)

### 👥 사용자 관리 엔드포인트
- `GET /users` - 사용자 목록 조회
- `POST /users` - 새 사용자 생성
- `GET /users/{username}` - 특정 사용자 조회
- `DELETE /users/{username}` - 사용자 삭제

### 🗝️ 공개키 관리 엔드포인트
- `GET /users/{username}/keys` - 사용자 공개키 목록
- `POST /users/{username}/keys` - 공개키 추가
- `DELETE /users/{username}/keys/{key_id}` - 공개키 삭제

### 🏥 시스템 엔드포인트
- `GET /health` - 헬스체크
- `GET /` - 서버 정보

## 📝 서버 테스트

### 종합 테스트

```bash
# 포트 포워딩 시작
make port-forward

# 새 터미널에서 종합 테스트 실행
chmod +x scripts/test_server.sh
./scripts/test_server.sh
```

### API 사용법 도움말

```bash
# API 사용법 예시 보기
make api-examples
```

### 개별 테스트

```bash
# 헬스체크
curl http://localhost:8080/health

# 인증 테스트
make test-password

# 사용자 관리 테스트
make add-user-api USER=testuser PASSWORD=testpass
make list-users-api
```

## 실제 사용 예시

### 시나리오 1: 새 개발자 온보딩

```bash
# 1. 새 사용자 생성
curl -X POST http://localhost:8080/users \
  -H "Content-Type: application/json" \
  -d '{"username":"john","password":"initial_password_123"}'

# 2. 개발자의 SSH 공개키 추가
curl -X POST http://localhost:8080/users/john/keys \
  -H "Content-Type: application/json" \
  -d '{
    "public_key": "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC...",
    "key_name": "john-laptop"
  }'

# 3. 확인
curl http://localhost:8080/users/john
curl http://localhost:8080/users/john/keys
```

### 시나리오 2: 대량 사용자 생성 스크립트

```bash
#!/bin/bash
# bulk_create_users.sh

users=("alice" "bob" "charlie")
for user in "${users[@]}"; do
  curl -X POST http://localhost:8080/users \
    -H "Content-Type: application/json" \
    -d "{\"username\":\"$user\",\"password\":\"temp_pass_123\"}"
done
```

## 개발 환경

### 로컬 개발

```bash
# 개발 의존성 설치
make dev

# 로컬 MySQL 실행
make dev-mysql

# 개발 서버 실행
make run

# 로컬에서 API 테스트
curl http://localhost:8000/docs
```

## 디렉토리와 파일 상세

| 파일/디렉토리 | 역할 | 주요 입력 | 주요 출력/효과 |
| --- | --- | --- | --- |
| `app/` | FastAPI 애플리케이션 코드입니다. 인증 API, 사용자/키 관리 API, DB model/service를 포함합니다. | ContainerSSH webhook 요청, REST API 요청, MySQL | 인증 결과 JSON, 사용자/키 DB 변경 |
| `k8s/` | 인증 서버와 MySQL을 Kubernetes에 배포하는 manifest입니다. | `kubectl apply`, Secret/ConfigMap 값 | Deployment, Service, MySQL 초기 schema |
| `scripts/` | 운영/테스트용 CLI와 shell script입니다. | CLI args, 포트포워딩된 API, DB 환경변수 | 사용자/키 변경, 테스트 결과, 보안 manifest 재생성 |
| `Dockerfile` | 인증 서버 컨테이너 이미지를 빌드합니다. | `requirements.txt`, `app/` | Python 3.11 slim 기반 uvicorn 이미지 |
| `Makefile` | 빌드, 배포, 포트포워딩, 테스트 API 호출 shortcut을 제공합니다. | make target, `USER`, `PASSWORD`, kubectl 컨텍스트 | Docker image, Kubernetes 배포/로그/테스트 실행 |
| `docker-compose.yml` | 로컬 개발용 MySQL, auth-server, Adminer 구성을 제공합니다. | Docker Compose | 로컬 MySQL/API/Adminer 컨테이너 |
| `requirements.txt` | FastAPI 서버 런타임 의존성입니다. | pip | FastAPI, uvicorn, SQLAlchemy, PyMySQL, passlib 등 설치 |
| `.dockerignore` | Docker build context 제외 규칙입니다. | Docker build | 불필요 파일 제외 |

자세한 클래스/함수/입출력은 각 하위 디렉토리 README를 참고하세요.

## 보안 고려사항

1. **API 접근 제어**: 프로덕션에서는 API 엔드포인트에 인증 추가 권장
2. **HTTPS 사용**: 프로덕션에서는 HTTPS 사용 필수
3. **패스워드 정책**: 강력한 패스워드 정책 적용 권장
4. **정기적 키 교체**: SSH 공개키 정기적 검토 및 교체

## 마이그레이션 가이드

### 기존 Makefile 명령어에서 API로

| 기존 명령어 | 새로운 API 방법 |
|-------------|----------------|
| `make add-user USER=test PASSWORD=test` | `make add-user-api USER=test PASSWORD=test` |
| `make list-users` | `make list-users-api` |
| 복잡한 스크립트 실행 | `curl http://localhost:8080/users` |

### 자동화 스크립트 업데이트

기존:
```bash
# 복잡한 kubectl + mysql 명령어들...
```

새로운 방법:
```bash
# 간단한 HTTP API 호출
curl -X POST http://localhost:8080/users -H "Content-Type: application/json" -d '{"username":"user","password":"pass"}'
```

## 문제 해결

### API 연결 실패
```bash
# 포트 포워딩 확인
make port-forward

# 서버 상태 확인
make status
make logs
```

### 데이터베이스 연결 문제
```bash
# MySQL 상태 확인
make logs-mysql

# 헬스체크로 확인
curl http://localhost:8080/health
```

## 라이선스

MIT License

---

🎉 **이제 복잡한 명령어 없이 브라우저나 간단한 curl 명령어로 사용자를 관리할 수 있습니다!**

- 📖 **API 문서**: http://localhost:8080/docs
- 🚀 **빠른 시작**: `make api-examples`
- 💬 **도움이 필요하면**: GitHub Issues에 문의하세요
