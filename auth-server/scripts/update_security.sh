#!/bin/bash

# ContainerSSH 인증 서버 보안 설정 업데이트 스크립트

echo "=== ContainerSSH 보안 설정 업데이트 ==="

# 색상 정의
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

# 필수 패키지 설치 확인 및 설치
check_and_install_dependencies() {
    echo "=== 의존성 확인 및 설치 ==="
    
    # Python 확인
    if ! command -v python3 &> /dev/null; then
        echo -e "${RED}✗ Python3이 설치되지 않았습니다. 먼저 Python3을 설치하세요.${NC}"
        exit 1
    fi
    
    # pip 확인
    if ! python3 -m pip --version &> /dev/null; then
        echo -e "${YELLOW}! pip가 설치되지 않았습니다. 설치를 시도합니다...${NC}"
        if command -v apt-get &> /dev/null; then
            sudo apt-get update && sudo apt-get install -y python3-pip
        elif command -v yum &> /dev/null; then
            sudo yum install -y python3-pip
        elif command -v dnf &> /dev/null; then
            sudo dnf install -y python3-pip
        else
            echo -e "${RED}✗ pip를 설치할 수 없습니다. 수동으로 설치하세요.${NC}"
            exit 1
        fi
    fi
    
    # passlib 확인 및 설치
    if ! python3 -c "import passlib" &> /dev/null; then
        echo -e "${YELLOW}! passlib가 설치되지 않았습니다. 설치합니다...${NC}"
        python3 -m pip install passlib[bcrypt] --user
        if [ $? -ne 0 ]; then
            echo -e "${RED}✗ passlib 설치 실패. 수동으로 설치하세요: pip install passlib[bcrypt]${NC}"
            exit 1
        fi
    fi
    
    # openssl 확인
    if ! command -v openssl &> /dev/null; then
        echo -e "${RED}✗ openssl이 설치되지 않았습니다. 설치하세요.${NC}"
        exit 1
    fi
    
    echo -e "${GREEN}✓ 모든 의존성 확인 완료${NC}"
}

# Base64 인코딩 함수
base64_encode() {
    echo -n "$1" | base64 | tr -d '\n'
}

# 패스워드 생성 함수
generate_password() {
    openssl rand -base64 32 | tr -d "=+/" | cut -c1-25
}

# 시작
check_and_install_dependencies

echo -e "${YELLOW}주의: 이 스크립트는 기존 데이터베이스를 초기화합니다!${NC}"
echo -e "${YELLOW}프로덕션 환경에서는 신중하게 사용하세요.${NC}"
echo ""

read -p "계속하시겠습니까? (y/N): " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "취소되었습니다."
    exit 1
fi

echo ""
echo "=== 1. MySQL 패스워드 생성 ==="

# 새 패스워드 생성
MYSQL_ROOT_PASSWORD=$(generate_password)
MYSQL_USER_PASSWORD=$(generate_password)

echo "Root 패스워드: $MYSQL_ROOT_PASSWORD"
echo "사용자 패스워드: $MYSQL_USER_PASSWORD"

# Base64 인코딩
ROOT_PASSWORD_B64=$(base64_encode "$MYSQL_ROOT_PASSWORD")
USER_PASSWORD_B64=$(base64_encode "$MYSQL_USER_PASSWORD")
DB_USER_B64=$(base64_encode "containerssh")
DB_NAME_B64=$(base64_encode "containerssh_auth")

echo ""
echo "=== 2. mysql-secret.yaml 업데이트 ==="

cat > k8s/mysql-secret.yaml << EOF
apiVersion: v1
kind: Secret
metadata:
  name: mysql-secret
  namespace: containerssh
type: Opaque
data:
  # Root 패스워드: $MYSQL_ROOT_PASSWORD (base64 인코딩)
  mysql-root-password: $ROOT_PASSWORD_B64
  # 데이터베이스 사용자: containerssh (base64 인코딩)
  mysql-user: $DB_USER_B64
  # 사용자 패스워드: $MYSQL_USER_PASSWORD (base64 인코딩)
  mysql-password: $USER_PASSWORD_B64
  # 데이터베이스 이름: containerssh_auth (base64 인코딩)
  mysql-database: $DB_NAME_B64
EOF

echo -e "${GREEN}✓ mysql-secret.yaml 업데이트 완료${NC}"

echo ""
echo "=== 3. 기본 사용자 패스워드 해시 생성 ==="

# 패스워드 입력 재시도 로직
while true; do
    read -sp "admin 사용자의 새 패스워드를 입력하세요: " ADMIN_PASSWORD
    echo
    if [ -n "$ADMIN_PASSWORD" ]; then
        break
    else
        echo -e "${RED}패스워드를 입력하세요.${NC}"
    fi
done

while true; do
    read -sp "user1 사용자의 새 패스워드를 입력하세요: " USER1_PASSWORD
    echo
    if [ -n "$USER1_PASSWORD" ]; then
        break
    else
        echo -e "${RED}패스워드를 입력하세요.${NC}"
    fi
done

# bcrypt 해시 생성 (오류 처리 포함)
echo "패스워드 해시 생성 중..."

ADMIN_HASH=$(python3 -c "
try:
    from passlib.context import CryptContext
    ctx = CryptContext(schemes=['bcrypt'])
    print(ctx.hash('$ADMIN_PASSWORD'))
except ImportError:
    print('ERROR: passlib not found')
    exit(1)
except Exception as e:
    print(f'ERROR: {e}')
    exit(1)
")

if [[ $ADMIN_HASH == ERROR* ]]; then
    echo -e "${RED}✗ admin 패스워드 해시 생성 실패: $ADMIN_HASH${NC}"
    exit 1
fi

USER1_HASH=$(python3 -c "
try:
    from passlib.context import CryptContext
    ctx = CryptContext(schemes=['bcrypt'])
    print(ctx.hash('$USER1_PASSWORD'))
except ImportError:
    print('ERROR: passlib not found')
    exit(1)
except Exception as e:
    print(f'ERROR: {e}')
    exit(1)
")

if [[ $USER1_HASH == ERROR* ]]; then
    echo -e "${RED}✗ user1 패스워드 해시 생성 실패: $USER1_HASH${NC}"
    exit 1
fi

echo -e "${GREEN}✓ 패스워드 해시 생성 완료${NC}"

echo ""
echo "=== 4. mysql-init-configmap.yaml 업데이트 ==="

cat > k8s/mysql-init-configmap.yaml << EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: mysql-init-config
  namespace: containerssh
data:
  init.sql: |
    CREATE DATABASE IF NOT EXISTS containerssh_auth;
    USE containerssh_auth;
    
    -- 사용자 테이블 생성
    CREATE TABLE IF NOT EXISTS users (
        id INT AUTO_INCREMENT PRIMARY KEY,
        username VARCHAR(50) NOT NULL UNIQUE,
        password_hash VARCHAR(255) NOT NULL,
        is_active TINYINT DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX idx_username (username),
        INDEX idx_active (is_active)
    );
    
    -- 사용자 공개키 테이블 생성
    CREATE TABLE IF NOT EXISTS user_keys (
        id INT AUTO_INCREMENT PRIMARY KEY,
        username VARCHAR(50) NOT NULL,
        public_key TEXT NOT NULL,
        key_name VARCHAR(100),
        is_active TINYINT DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_username (username),
        INDEX idx_active (is_active)
    );
    
    -- 기본 사용자 추가 (새로운 해시된 패스워드)
    INSERT IGNORE INTO users (username, password_hash) VALUES 
    ('admin', '$ADMIN_HASH'),
    ('user1', '$USER1_HASH');
    
    -- 주의: 아래의 공개키들은 예시입니다. 실제 공개키로 교체하세요!
    INSERT IGNORE INTO user_keys (username, public_key, key_name) VALUES 
    ('admin', 'ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC7vbqajDhj...', 'admin-key-REPLACE-ME'),
    ('user1', 'ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQD8xyz123...', 'user1-key-REPLACE-ME');
    
    FLUSH PRIVILEGES;
EOF

echo -e "${GREEN}✓ mysql-init-configmap.yaml 업데이트 완료${NC}"

echo ""
echo "=== 5. MySQL Deployment의 하드코딩된 패스워드 수정 ==="

# mysql-deployment.yaml의 하드코딩된 패스워드 수정
if [ -f "k8s/mysql-deployment.yaml" ]; then
    sed -i "s/mysqladmin ping -h localhost -u root -prootpassword123/mysqladmin ping -h localhost -u root -p\$MYSQL_ROOT_PASSWORD/g" k8s/mysql-deployment.yaml
    echo -e "${GREEN}✓ mysql-deployment.yaml 업데이트 완료${NC}"
else
    echo -e "${YELLOW}! mysql-deployment.yaml 파일을 찾을 수 없습니다.${NC}"
fi

echo ""
echo "=== 보안 업데이트 완료 ==="
echo -e "${GREEN}✓ 모든 보안 설정이 업데이트되었습니다.${NC}"
echo ""
echo -e "${YELLOW}중요: 다음 작업을 반드시 수행하세요:${NC}"
echo "1. k8s/mysql-init-configmap.yaml에서 SSH 공개키를 실제 키로 교체"
echo "2. 기존 배포 삭제 후 재배포:"
echo "   make clean"
echo "   make deploy"
echo ""
echo "3. 새 패스워드 정보를 안전한 곳에 저장:"
echo "   MySQL Root: $MYSQL_ROOT_PASSWORD"
echo "   MySQL User: $MYSQL_USER_PASSWORD"
echo "   Admin User: (입력한 패스워드)"
echo "   User1: (입력한 패스워드)"
echo ""
echo -e "${RED}보안상 이 터미널 히스토리를 정리하는 것을 권장합니다.${NC}"
echo ""
echo "=== 다음 단계 ==="
echo "1. make clean"
echo "2. make deploy"
echo "3. make port-forward"
echo "4. 새 패스워드로 테스트: ./scripts/test_server.sh"
