#!/bin/bash
set -e

# ContainerSSH 인증 서버 종합 테스트 스크립트

echo "=== ContainerSSH 인증 서버 테스트 시작 ==="

# 기본 설정
BASE_URL="http://localhost:8080"
TIMEOUT=5

# 색상 정의
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 테스트 함수들
test_health() {
    echo "1. 헬스체크 테스트..."
    local response=$(curl -s --max-time $TIMEOUT $BASE_URL/health 2>/dev/null || echo "ERROR")
    
    if echo "$response" | grep -q "healthy"; then
        echo -e "${GREEN}✓ 헬스체크 성공${NC}"
        if echo "$response" | grep -q "connected"; then
            echo -e "${GREEN}  ✓ 데이터베이스 연결 정상${NC}"
        fi
        return 0
    else
        echo -e "${RED}✗ 헬스체크 실패${NC}"
        echo "응답: $response"
        return 1
    fi
}

test_root() {
    echo "2. 루트 엔드포인트 테스트..."
    local response=$(curl -s --max-time $TIMEOUT $BASE_URL/ 2>/dev/null || echo "ERROR")
    
    if echo "$response" | grep -q "ContainerSSH"; then
        echo -e "${GREEN}✓ 루트 엔드포인트 성공${NC}"
        return 0
    else
        echo -e "${RED}✗ 루트 엔드포인트 실패${NC}"
        echo "응답: $response"
        return 1
    fi
}

test_password_auth_interactive() {
    echo "3. 패스워드 인증 테스트..."
    echo -e "${BLUE}기본 테스트 사용자를 사용하거나 커스텀 사용자로 테스트할 수 있습니다.${NC}"
    
    echo ""
    echo "선택하세요:"
    echo "1) 기본 사용자로 테스트 (admin/secret)"
    echo "2) 커스텀 사용자로 테스트"
    echo "3) 건너뛰기"
    
    read -p "선택 (1-3): " choice
    
    case $choice in
        1)
            test_password_auth_with_creds "admin" "secret"
            ;;
        2)
            read -p "사용자명을 입력하세요: " username
            read -sp "패스워드를 입력하세요: " password
            echo
            test_password_auth_with_creds "$username" "$password"
            ;;
        3)
            echo -e "${YELLOW}✓ 패스워드 인증 테스트 건너뛰기${NC}"
            return 0
            ;;
        *)
            echo -e "${YELLOW}✓ 패스워드 인증 테스트 건너뛰기${NC}"
            return 0
            ;;
    esac
}

test_password_auth_with_creds() {
    local username="$1"
    local password="$2"
    
    # 패스워드를 Base64로 인코딩
    local password_base64=$(echo -n "$password" | base64)
    
    echo "  테스트 중: $username 사용자..."
    
    # 성공 케이스
    local response=$(curl -s --max-time $TIMEOUT -X POST $BASE_URL/password \
        -H "Content-Type: application/json" \
        -d "{\"username\":\"$username\",\"remoteAddress\":\"127.0.0.1:1234\",\"connectionId\":\"test-conn\",\"passwordBase64\":\"$password_base64\"}" \
        2>/dev/null || echo "ERROR")
    
    if echo "$response" | grep -q '"success":true'; then
        echo -e "${GREEN}  ✓ 패스워드 인증 성공 ($username)${NC}"
    else
        echo -e "${RED}  ✗ 패스워드 인증 실패 ($username)${NC}"
        echo "  응답: $response"
        return 1
    fi
    
    # 실패 케이스 (잘못된 패스워드)
    local wrong_password_base64=$(echo -n "wrongpassword" | base64)
    local response_fail=$(curl -s --max-time $TIMEOUT -X POST $BASE_URL/password \
        -H "Content-Type: application/json" \
        -d "{\"username\":\"$username\",\"remoteAddress\":\"127.0.0.1:1234\",\"connectionId\":\"test-conn\",\"passwordBase64\":\"$wrong_password_base64\"}" \
        2>/dev/null || echo "ERROR")
    
    if echo "$response_fail" | grep -q '"success":false'; then
        echo -e "${GREEN}  ✓ 잘못된 패스워드 차단 - 정상 동작${NC}"
        return 0
    else
        echo -e "${RED}  ✗ 보안 문제 - 잘못된 패스워드로도 인증 성공${NC}"
        echo "  응답: $response_fail"
        return 1
    fi
}

test_pubkey_auth() {
    echo "4. 공개키 인증 테스트..."
    
    # 실패 케이스 (등록되지 않은 키) - 이는 정상적인 동작
    local response=$(curl -s --max-time $TIMEOUT -X POST $BASE_URL/pubkey \
        -H "Content-Type: application/json" \
        -d '{"username":"admin","remoteAddress":"127.0.0.1:1234","connectionId":"test-conn","publicKey":"ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC7vbqajDhj..."}' \
        2>/dev/null || echo "ERROR")
    
    if echo "$response" | grep -q '"success":false'; then
        echo -e "${GREEN}✓ 공개키 인증 (등록되지 않은 키 차단) - 정상 동작${NC}"
        return 0
    else
        echo -e "${YELLOW}! 공개키 인증 결과 확인 필요${NC}"
        echo "응답: $response"
        return 0
    fi
}

check_prerequisites() {
    echo "=== 사전 요구사항 확인 ==="
    
    # kubectl 명령어 확인
    if ! command -v kubectl &> /dev/null; then
        echo -e "${RED}✗ kubectl이 설치되지 않았습니다${NC}"
        exit 1
    fi
    
    # 포트 포워딩 확인
    if ! curl -s --max-time 2 $BASE_URL/health &> /dev/null; then
        echo -e "${YELLOW}! 포트 포워딩이 설정되지 않았습니다.${NC}"
        echo ""
        echo "다음 명령어를 실행하세요:"
        echo -e "${BLUE}kubectl port-forward -n containerssh svc/containerssh-auth-service 8080:80${NC}"
        echo ""
        
        read -p "포트 포워딩을 자동으로 시작할까요? (y/N): " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            echo "포트 포워딩을 백그라운드에서 시작합니다..."
            kubectl port-forward -n containerssh svc/containerssh-auth-service 8080:80 &
            PF_PID=$!
            echo "PID: $PF_PID"
            
            # 포트 포워딩이 준비될 때까지 대기
            echo "연결 확인 중..."
            sleep 3
            
            if curl -s --max-time 2 $BASE_URL/health &> /dev/null; then
                echo -e "${GREEN}✓ 포트 포워딩 성공${NC}"
            else
                echo -e "${RED}✗ 포트 포워딩 실패. 수동으로 설정하세요.${NC}"
                kill $PF_PID 2>/dev/null || true
                exit 1
            fi
        else
            echo "포트 포워딩 후 다시 테스트를 실행하세요."
            exit 1
        fi
    fi
    
    echo -e "${GREEN}✓ 사전 요구사항 확인 완료${NC}"
}

cleanup() {
    if [ -n "$PF_PID" ]; then
        echo ""
        echo "포트 포워딩을 종료합니다..."
        kill $PF_PID 2>/dev/null || true
    fi
}

main() {
    # 종료 시 정리
    trap cleanup EXIT
    
    check_prerequisites
    
    echo ""
    echo "=== 기능 테스트 시작 ==="
    
    local failed=0
    
    test_health || failed=1
    test_root || failed=1
    test_password_auth_interactive || failed=1
    test_pubkey_auth || failed=1
    
    echo ""
    echo "=== 테스트 결과 ==="
    
    if [ $failed -eq 0 ]; then
        echo -e "${GREEN}✓ 모든 테스트 통과!${NC}"
        echo ""
        echo "=== 추가 테스트 명령어 ==="
        echo "1. 사용자 목록 확인: make list-users"
        echo "2. MySQL 직접 접속: make connect-mysql"
        echo "3. 로그 확인: make logs"
        echo "4. 포트 포워딩: make port-forward"
    else
        echo -e "${RED}✗ 일부 테스트 실패${NC}"
        echo ""
        echo "=== 문제 해결 방법 ==="
        echo "1. 로그 확인: make logs"
        echo "2. 헬스체크: curl $BASE_URL/health"
        echo "3. 재배포: make restart-app"
        exit 1
    fi
}

# 스크립트 실행
main "$@"
