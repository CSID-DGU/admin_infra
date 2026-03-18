.PHONY: help build run test deploy clean dev deploy-mysql deploy-app api-examples

# 기본 변수
IMAGE_NAME = containerssh-auth-server
IMAGE_TAG = latest
NAMESPACE = containerssh

help: ## 도움말 표시
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-30s\033[0m %s\n", $$1, $$2}'

build: ## Docker 이미지 빌드
	docker build -t $(IMAGE_NAME):$(IMAGE_TAG) .

run: ## 로컬에서 개발 서버 실행 (MySQL 없이)
	uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

test: ## 테스트 실행 (간단한 헬스체크)
	curl -f http://localhost:8000/health || exit 1

dev: ## 개발 환경 셋업
	pip install -r requirements.txt

deploy-mysql: ## MySQL만 배포
	@echo "네임스페이스 생성..."
	kubectl apply -f k8s/namespace.yaml
	@echo "MySQL Secret 적용..."
	kubectl apply -f k8s/mysql-secret.yaml
	@echo "MySQL ConfigMap 적용..."
	kubectl apply -f k8s/mysql-configmap.yaml
	kubectl apply -f k8s/mysql-init-configmap.yaml
	@echo "MySQL Deployment 적용..."
	kubectl apply -f k8s/mysql-deployment.yaml
	@echo "MySQL Service 적용..."
	kubectl apply -f k8s/mysql-service.yaml
	@echo "MySQL 배포 완료! MySQL이 준비될 때까지 잠시 기다려주세요..."

deploy-app: build ## 인증 서버만 배포
	@echo "이미지를 클러스터에 로드 중..."
	@if command -v kind >/dev/null 2>&1; then \
		echo "Kind 클러스터 감지됨. 이미지 로드 중..."; \
		kind load docker-image $(IMAGE_NAME):$(IMAGE_TAG); \
	elif command -v minikube >/dev/null 2>&1; then \
		echo "Minikube 클러스터 감지됨. Docker 환경 설정 중..."; \
		eval $(minikube docker-env); \
		docker build -t $(IMAGE_NAME):$(IMAGE_TAG) .; \
	else \
		echo "일반 Kubernetes 클러스터에서 실행 중. 모든 노드에 이미지가 있는지 확인하세요."; \
	fi
	@echo "ConfigMap 적용..."
	kubectl apply -f k8s/configmap.yaml
	@echo "Deployment 적용..."
	kubectl apply -f k8s/deployment.yaml
	@echo "Service 적용..."
	kubectl apply -f k8s/service.yaml
	@echo "인증 서버 배포 완료!"

deploy: deploy-mysql deploy-app ## 전체 배포 (MySQL + 인증 서버)
	@echo "전체 배포 완료!"

status: ## 배포 상태 확인
	kubectl get all -n $(NAMESPACE)

logs: ## 인증 서버 로그 확인
	kubectl logs -n $(NAMESPACE) -l app=containerssh-auth-server -f

logs-mysql: ## MySQL 로그 확인
	kubectl logs -n $(NAMESPACE) -l app=mysql -f

clean: ## 정리
	kubectl delete namespace $(NAMESPACE) || true
	docker rmi $(IMAGE_NAME):$(IMAGE_TAG) || true

port-forward: ## 포트 포워딩 (API 사용)
	kubectl port-forward -n $(NAMESPACE) svc/containerssh-auth-service 8080:80

port-forward-mysql: ## MySQL 포트 포워딩 (디버그용)
	kubectl port-forward -n $(NAMESPACE) svc/mysql-service 3306:3306

wait-for-mysql: ## MySQL 준비 상태 대기
	@echo "MySQL이 준비될 때까지 대기 중..."
	@kubectl wait --for=condition=ready pod -l app=mysql -n $(NAMESPACE) --timeout=300s
	@echo "MySQL이 준비되었습니다!"

restart-app: ## 인증 서버 재시작
	kubectl rollout restart deployment/containerssh-auth-server -n $(NAMESPACE)

restart-mysql: ## MySQL 재시작
	kubectl rollout restart deployment/mysql -n $(NAMESPACE)

api-examples: ## API 사용법 예시 표시
	@echo "=== ContainerSSH 인증 서버 API 사용법 ==="
	@echo ""
	@echo "🔧 포트 포워딩 시작:"
	@echo "  make port-forward"
	@echo ""
	@echo "📋 사용자 목록 조회:"
	@echo "  curl http://localhost:8080/users"
	@echo ""
	@echo "👤 새 사용자 추가:"
	@echo "  curl -X POST http://localhost:8080/users \\"
	@echo "    -H \"Content-Type: application/json\" \\"
	@echo "    -d '{\"username\":\"test123\",\"password\":\"test123\"}'"
	@echo ""
	@echo "🔍 특정 사용자 조회:"
	@echo "  curl http://localhost:8080/users/test123"
	@echo ""
	@echo "🔑 공개키 목록 조회:"
	@echo "  curl http://localhost:8080/users/test123/keys"
	@echo ""
	@echo "➕ 공개키 추가:"
	@echo "  curl -X POST http://localhost:8080/users/test123/keys \\"
	@echo "    -H \"Content-Type: application/json\" \\"
	@echo "    -d '{\"public_key\":\"ssh-rsa AAAAB3...\",\"key_name\":\"laptop\"}'"
	@echo ""
	@echo "🗑️  사용자 삭제:"
	@echo "  curl -X DELETE http://localhost:8080/users/test123"
	@echo ""
	@echo "🏥 헬스체크:"
	@echo "  curl http://localhost:8080/health"
	@echo ""
	@echo "📊 API 문서 (Swagger UI):"
	@echo "  http://localhost:8080/docs"
	@echo ""
	@echo "📄 API 스키마 (ReDoc):"
	@echo "  http://localhost:8080/redoc"

add-user-api: ## API를 통한 사용자 추가 (USER=username PASSWORD=password)
	@if [ -z "$(USER)" ] || [ -z "$(PASSWORD)" ]; then \
		echo "사용법: make add-user-api USER=username PASSWORD=password"; \
		echo "예시: make add-user-api USER=test123 PASSWORD=test123"; \
		exit 1; \
	fi
	@echo "API를 통해 사용자 $(USER) 추가 중..."
	@curl -X POST http://localhost:8080/users \
		-H "Content-Type: application/json" \
		-d '{"username":"$(USER)","password":"$(PASSWORD)"}' \
		-w "\n" || echo "❌ 포트 포워딩이 필요합니다: make port-forward"

list-users-api: ## API를 통한 사용자 목록 조회
	@echo "API를 통해 사용자 목록 조회 중..."
	@curl http://localhost:8080/users -w "\n" || echo "❌ 포트 포워딩이 필요합니다: make port-forward"

test-password: ## 패스워드 인증 테스트
	curl -X POST http://localhost:8080/password \
		-H "Content-Type: application/json" \
		-d '{"username":"admin","remoteAddress":"127.0.0.1:1234","connectionId":"test-conn","passwordBase64":"c2VjcmV0"}'

test-pubkey: ## 공개키 인증 테스트
	curl -X POST http://localhost:8080/pubkey \
		-H "Content-Type: application/json" \
		-d '{"username":"admin","remoteAddress":"127.0.0.1:1234","connectionId":"test-conn","publicKey":"ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC..."}'

dev-mysql: ## 로컬 MySQL 컨테이너 실행 (개발용)
	docker run --name mysql-dev -e MYSQL_ROOT_PASSWORD=rootpassword123 -e MYSQL_DATABASE=containerssh_auth -e MYSQL_USER=containerssh -e MYSQL_PASSWORD=containerssh123 -p 3306:3306 -d mysql:8.0

dev-mysql-stop: ## 로컬 MySQL 컨테이너 중지
	docker stop mysql-dev && docker rm mysql-dev
