from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import JSONResponse
from typing import List, Optional
import logging
from .models import (
    PasswordAuthRequest, PublicKeyAuthRequest, AuthResponse,
    UserCreateRequest, UserResponse, UserKeyCreateRequest, UserKeyResponse
)
from .auth import AuthService
from .database import (
    init_db, test_connection, get_db, get_user_by_username, 
    get_user_keys, create_user, add_user_key, Session
)
from passlib.context import CryptContext
from sqlalchemy import text

# 로깅 설정
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="ContainerSSH Authentication Server",
    description="Authentication server for ContainerSSH with MySQL backend and User Management",
    version="1.1.0"
)

auth_service = AuthService()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

@app.on_event("startup")
async def startup_event():
    """애플리케이션 시작 시 데이터베이스 초기화"""
    logger.info("Starting application...")
    
    # 데이터베이스 연결 테스트
    if not test_connection():
        logger.error("Database connection failed during startup")
        raise Exception("Database connection failed")
    
    # 데이터베이스 테이블 초기화
    try:
        init_db()
        logger.info("Database initialization completed")
    except Exception as e:
        logger.error(f"Database initialization failed: {str(e)}")
        raise

@app.get("/health")
async def health_check():
    """헬스체크 엔드포인트"""
    # 데이터베이스 연결 상태도 확인
    db_status = test_connection()
    return {
        "status": "healthy" if db_status else "unhealthy",
        "database": "connected" if db_status else "disconnected"
    }

# ===== 인증 엔드포인트 =====
@app.post("/password")
async def password_auth(request: PasswordAuthRequest):
    """ContainerSSH 패스워드 인증 엔드포인트"""
    logger.info(f"Password authentication request for user: {request.username}")

    success, authenticated_username = auth_service.verify_password(
        request.username,
        request.passwordBase64,
        request.remoteAddress,
        request.connectionId
    )

    response = AuthResponse(
        success=success,
        authenticatedUsername=authenticated_username if success else None
    )

    return response

@app.post("/pubkey")
async def pubkey_auth(request: PublicKeyAuthRequest):
    """ContainerSSH 공개키 인증 엔드포인트"""
    logger.info(f"Public key authentication request for user: {request.username}")

    success, authenticated_username = auth_service.verify_public_key(
        request.username,
        request.publicKey,
        request.remoteAddress,
        request.connectionId
    )

    response = AuthResponse(
        success=success,
        authenticatedUsername=authenticated_username if success else None
    )

    return response

# ===== 사용자 관리 엔드포인트 =====
@app.get("/users", response_model=List[UserResponse])
async def list_users(db: Session = Depends(get_db)):
    """사용자 목록 조회"""
    try:
        result = db.execute(
            text("SELECT id, username, is_active, created_at FROM users ORDER BY id")
        ).fetchall()
        
        users = []
        for row in result:
            users.append(UserResponse(
                id=row[0],
                username=row[1],
                is_active=bool(row[2]),
                created_at=row[3]
            ))
        
        return users
    except Exception as e:
        logger.error(f"Error listing users: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to list users")

@app.post("/users", response_model=UserResponse)
async def create_user_endpoint(request: UserCreateRequest, db: Session = Depends(get_db)):
    """새 사용자 생성"""
    try:
        # 사용자 존재 확인
        existing_user = get_user_by_username(db, request.username)
        if existing_user:
            raise HTTPException(status_code=400, detail="User already exists")
        
        # 패스워드 해싱
        password_hash = pwd_context.hash(request.password)
        
        # 사용자 생성
        db.execute(
            text("INSERT INTO users (username, password_hash) VALUES (:username, :password_hash)"),
            {"username": request.username, "password_hash": password_hash}
        )
        db.commit()
        
        # 생성된 사용자 조회
        new_user = get_user_by_username(db, request.username)
        
        logger.info(f"User created: {request.username}")
        
        return UserResponse(
            id=new_user.id,
            username=new_user.username,
            is_active=bool(new_user.is_active),
            created_at=new_user.created_at
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating user: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to create user")

@app.get("/users/{username}", response_model=UserResponse)
async def get_user(username: str, db: Session = Depends(get_db)):
    """특정 사용자 조회"""
    user = get_user_by_username(db, username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    return UserResponse(
        id=user.id,
        username=user.username,
        is_active=bool(user.is_active),
        created_at=user.created_at
    )

@app.delete("/users/{username}")
async def delete_user(username: str, db: Session = Depends(get_db)):
    """사용자 삭제 (비활성화)"""
    try:
        user = get_user_by_username(db, username)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        # 사용자 비활성화
        db.execute(
            text("UPDATE users SET is_active = 0 WHERE username = :username"),
            {"username": username}
        )
        
        # 사용자 공개키 비활성화
        db.execute(
            text("UPDATE user_keys SET is_active = 0 WHERE username = :username"),
            {"username": username}
        )
        
        db.commit()
        
        logger.info(f"User deactivated: {username}")
        
        return {"message": f"User {username} deactivated successfully"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting user: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to delete user")

# ===== 공개키 관리 엔드포인트 =====
@app.get("/users/{username}/keys", response_model=List[UserKeyResponse])
async def list_user_keys(username: str, db: Session = Depends(get_db)):
    """사용자 공개키 목록 조회"""
    try:
        # 사용자 존재 확인
        user = get_user_by_username(db, username)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        # 공개키 목록 조회
        keys = get_user_keys(db, username)
        
        result = []
        for key in keys:
            result.append(UserKeyResponse(
                id=key.id,
                username=key.username,
                public_key=key.public_key,
                key_name=key.key_name,
                is_active=bool(key.is_active),
                created_at=key.created_at
            ))
        
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing user keys: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to list user keys")

@app.post("/users/{username}/keys", response_model=UserKeyResponse)
async def add_user_key_endpoint(username: str, request: UserKeyCreateRequest, db: Session = Depends(get_db)):
    """사용자 공개키 추가"""
    try:
        # 사용자 존재 확인
        user = get_user_by_username(db, username)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        
        # 공개키 추가
        db.execute(
            text("INSERT INTO user_keys (username, public_key, key_name) VALUES (:username, :public_key, :key_name)"),
            {"username": username, "public_key": request.public_key, "key_name": request.key_name}
        )
        db.commit()
        
        # 추가된 키 조회
        result = db.execute(
            text("SELECT id, username, public_key, key_name, is_active, created_at FROM user_keys WHERE username = :username ORDER BY id DESC LIMIT 1"),
            {"username": username}
        ).fetchone()
        
        logger.info(f"Public key added for user: {username}")
        
        return UserKeyResponse(
            id=result[0],
            username=result[1],
            public_key=result[2],
            key_name=result[3],
            is_active=bool(result[4]),
            created_at=result[5]
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error adding user key: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to add user key")

@app.delete("/users/{username}/keys/{key_id}")
async def delete_user_key(username: str, key_id: int, db: Session = Depends(get_db)):
    """사용자 공개키 삭제 (비활성화)"""
    try:
        # 키 존재 확인
        result = db.execute(
            text("SELECT COUNT(*) FROM user_keys WHERE id = :key_id AND username = :username"),
            {"key_id": key_id, "username": username}
        ).fetchone()
        
        if result[0] == 0:
            raise HTTPException(status_code=404, detail="Key not found")
        
        # 키 비활성화
        db.execute(
            text("UPDATE user_keys SET is_active = 0 WHERE id = :key_id AND username = :username"),
            {"key_id": key_id, "username": username}
        )
        db.commit()
        
        logger.info(f"Public key {key_id} deactivated for user: {username}")
        
        return {"message": f"Key {key_id} deactivated successfully"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting user key: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to delete user key")

@app.get("/")
async def root():
    """루트 엔드포인트"""
    return {
        "message": "ContainerSSH Authentication Server with MySQL",
        "version": "1.1.0",
        "database": "MySQL",
        "features": [
            "Password Authentication",
            "Public Key Authentication", 
            "User Management API",
            "Public Key Management API"
        ],
        "endpoints": {
            "auth": ["/password", "/pubkey"],
            "users": ["/users", "/users/{username}"],
            "keys": ["/users/{username}/keys"]
        }
    }
