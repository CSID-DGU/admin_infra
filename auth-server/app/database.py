import os
import logging
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session
from datetime import datetime
from typing import Optional, List, Generator

logger = logging.getLogger(__name__)

# 데이터베이스 URL 구성
DB_HOST = os.getenv("DB_HOST", "mysql-service")
DB_PORT = os.getenv("DB_PORT", "3306")
DB_NAME = os.getenv("DB_NAME", "containerssh_auth")
DB_USER = os.getenv("DB_USER", "containerssh")
DB_PASSWORD = os.getenv("DB_PASSWORD", "containerssh123")

DATABASE_URL = f"mysql+pymysql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# SQLAlchemy 설정
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_recycle=300,
    echo=False
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# 데이터베이스 모델
class User(Base):
    __tablename__ = "users"
    
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False)  # bcrypt 해시
    is_active = Column(Integer, default=1)  # 0: 비활성, 1: 활성
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class UserKey(Base):
    __tablename__ = "user_keys"
    
    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), index=True, nullable=False)
    public_key = Column(Text, nullable=False)
    key_name = Column(String(100))  # 키 이름/설명
    is_active = Column(Integer, default=1)  # 0: 비활성, 1: 활성
    created_at = Column(DateTime, default=datetime.utcnow)

# FastAPI 의존성 주입을 위한 get_db 제너레이터
def get_db() -> Generator[Session, None, None]:
    """FastAPI 의존성 주입용 데이터베이스 세션 생성"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# 직접 세션을 반환하는 기존 함수 (auth.py에서 사용)
def get_db_session() -> Session:
    """직접 세션을 반환하는 함수 (auth.py 호환성을 위해 유지)"""
    return SessionLocal()

def init_db():
    """데이터베이스 테이블 생성"""
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("Database tables created successfully")
    except Exception as e:
        logger.error(f"Database initialization failed: {str(e)}")
        raise

def test_connection():
    """데이터베이스 연결 테스트"""
    try:
        db = SessionLocal()
        db.execute(text("SELECT 1"))
        db.close()
        logger.info("Database connection test successful")
        return True
    except Exception as e:
        logger.error(f"Database connection test failed: {str(e)}")
        return False

# 데이터베이스 작업 함수들
def get_user_by_username(db: Session, username: str) -> Optional[User]:
    """사용자명으로 사용자 조회"""
    return db.query(User).filter(User.username == username, User.is_active == 1).first()

def get_user_keys(db: Session, username: str) -> List[UserKey]:
    """사용자의 공개키 목록 조회"""
    return db.query(UserKey).filter(UserKey.username == username, UserKey.is_active == 1).all()

def create_user(db: Session, username: str, password_hash: str) -> User:
    """새 사용자 생성"""
    db_user = User(username=username, password_hash=password_hash)
    db.add(db_user)
    db.commit()
    db.refresh(db_user)
    return db_user

def add_user_key(db: Session, username: str, public_key: str, key_name: str = None) -> UserKey:
    """사용자 공개키 추가"""
    db_key = UserKey(username=username, public_key=public_key, key_name=key_name)
    db.add(db_key)
    db.commit()
    db.refresh(db_key)
    return db_key
