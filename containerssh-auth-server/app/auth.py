import base64
import logging
from passlib.context import CryptContext
from typing import Optional
from sqlalchemy.orm import Session
from .database import get_db_session, get_user_by_username, get_user_keys

# 로깅 설정
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 패스워드 해싱 컨텍스트
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

class AuthService:
    def __init__(self):
        pass

    def verify_password(self, username: str, password_base64: str, remote_address: str, connection_id: str) -> tuple[bool, Optional[str]]:
        """
        패스워드 인증 검증 (데이터베이스 기반)
        """
        try:
            # Base64 디코딩
            password = base64.b64decode(password_base64).decode('utf-8')

            logger.info(f"Password auth attempt for user: {username} from {remote_address} (conn: {connection_id})")

            # 데이터베이스에서 사용자 조회
            db = get_db_session()
            try:
                user = get_user_by_username(db, username)
                if not user:
                    logger.warning(f"Unknown user: {username}")
                    return False, None

                # 패스워드 검증
                if pwd_context.verify(password, user.password_hash):
                    logger.info(f"Password authentication successful for user: {username}")
                    return True, username
                else:
                    logger.warning(f"Password authentication failed for user: {username}")
                    return False, None

            finally:
                db.close()

        except Exception as e:
            logger.error(f"Password authentication error: {str(e)}")
            return False, None

    def verify_public_key(self, username: str, public_key: str, remote_address: str, connection_id: str) -> tuple[bool, Optional[str]]:
        """
        공개키 인증 검증 (데이터베이스 기반)
        """
        try:
            logger.info(f"Public key auth attempt for user: {username} from {remote_address} (conn: {connection_id})")

            # 데이터베이스에서 사용자 존재 확인
            db = get_db_session()
            try:
                user = get_user_by_username(db, username)
                if not user:
                    logger.warning(f"Unknown user: {username}")
                    return False, None

                # 사용자의 공개키 목록 조회
                user_keys = get_user_keys(db, username)
                if not user_keys:
                    logger.warning(f"No public keys found for user: {username}")
                    return False, None

                # 공개키 검증
                public_key_clean = public_key.strip()
                for user_key in user_keys:
                    if user_key.public_key.strip() == public_key_clean:
                        logger.info(f"Public key authentication successful for user: {username}")
                        return True, username

                logger.warning(f"Public key authentication failed for user: {username}")
                return False, None

            finally:
                db.close()

        except Exception as e:
            logger.error(f"Public key authentication error: {str(e)}")
            return False, None
