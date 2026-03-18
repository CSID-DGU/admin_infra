#!/usr/bin/env python3
"""
ContainerSSH 인증 서버 사용자 관리 스크립트

사용법:
    python manage_users.py add-user --username admin --password secret
    python manage_users.py list-users
    python manage_users.py add-key --username admin --key "ssh-rsa AAAA..." --name "admin-laptop"
    python manage_users.py list-keys --username admin
    python manage_users.py delete-user --username user1
"""

import argparse
import sys
import os
from passlib.context import CryptContext
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# 데이터베이스 연결 설정
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "3306")
DB_NAME = os.getenv("DB_NAME", "containerssh_auth")
DB_USER = os.getenv("DB_USER", "containerssh")
DB_PASSWORD = os.getenv("DB_PASSWORD", "containerssh123")

DATABASE_URL = f"mysql+pymysql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

# 패스워드 해싱
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

def get_db_session():
    """데이터베이스 세션 생성"""
    engine = create_engine(DATABASE_URL)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return SessionLocal()

def add_user(username, password):
    """사용자 추가"""
    try:
        session = get_db_session()
        
        # 사용자 존재 확인
        result = session.execute(
            text("SELECT COUNT(*) FROM users WHERE username = :username"),
            {"username": username}
        ).fetchone()
        
        if result[0] > 0:
            print(f"오류: 사용자 '{username}'이 이미 존재합니다.")
            return False
        
        # 패스워드 해싱
        password_hash = pwd_context.hash(password)
        
        # 사용자 추가
        session.execute(
            text("INSERT INTO users (username, password_hash) VALUES (:username, :password_hash)"),
            {"username": username, "password_hash": password_hash}
        )
        session.commit()
        
        print(f"사용자 '{username}'이 성공적으로 추가되었습니다.")
        return True
        
    except Exception as e:
        print(f"오류: 사용자 추가 실패 - {str(e)}")
        return False
    finally:
        session.close()

def list_users():
    """사용자 목록 조회"""
    try:
        session = get_db_session()
        
        result = session.execute(
            text("SELECT id, username, is_active, created_at FROM users ORDER BY id")
        ).fetchall()
        
        if not result:
            print("등록된 사용자가 없습니다.")
            return
        
        print(f"{'ID':<5} {'사용자명':<20} {'활성상태':<10} {'생성일':<20}")
        print("-" * 60)
        
        for row in result:
            status = "활성" if row[2] else "비활성"
            print(f"{row[0]:<5} {row[1]:<20} {status:<10} {row[3]}")
            
    except Exception as e:
        print(f"오류: 사용자 목록 조회 실패 - {str(e)}")
    finally:
        session.close()

def add_key(username, public_key, key_name=None):
    """사용자 공개키 추가"""
    try:
        session = get_db_session()
        
        # 사용자 존재 확인
        result = session.execute(
            text("SELECT COUNT(*) FROM users WHERE username = :username AND is_active = 1"),
            {"username": username}
        ).fetchone()
        
        if result[0] == 0:
            print(f"오류: 활성 사용자 '{username}'을 찾을 수 없습니다.")
            return False
        
        # 공개키 추가
        session.execute(
            text("INSERT INTO user_keys (username, public_key, key_name) VALUES (:username, :public_key, :key_name)"),
            {"username": username, "public_key": public_key, "key_name": key_name}
        )
        session.commit()
        
        print(f"사용자 '{username}'에 공개키가 성공적으로 추가되었습니다.")
        return True
        
    except Exception as e:
        print(f"오류: 공개키 추가 실패 - {str(e)}")
        return False
    finally:
        session.close()

def list_keys(username):
    """사용자 공개키 목록 조회"""
    try:
        session = get_db_session()
        
        result = session.execute(
            text("SELECT id, key_name, LEFT(public_key, 50), is_active, created_at FROM user_keys WHERE username = :username ORDER BY id"),
            {"username": username}
        ).fetchall()
        
        if not result:
            print(f"사용자 '{username}'의 공개키가 없습니다.")
            return
        
        print(f"사용자 '{username}'의 공개키 목록:")
        print(f"{'ID':<5} {'키 이름':<20} {'공개키 (일부)':<50} {'활성상태':<10} {'생성일':<20}")
        print("-" * 110)
        
        for row in result:
            key_name = row[1] or "(이름 없음)"
            status = "활성" if row[3] else "비활성"
            print(f"{row[0]:<5} {key_name:<20} {row[2]}... {status:<10} {row[4]}")
            
    except Exception as e:
        print(f"오류: 공개키 목록 조회 실패 - {str(e)}")
    finally:
        session.close()

def delete_user(username):
    """사용자 삭제 (비활성화)"""
    try:
        session = get_db_session()
        
        # 사용자 존재 확인
        result = session.execute(
            text("SELECT COUNT(*) FROM users WHERE username = :username"),
            {"username": username}
        ).fetchone()
        
        if result[0] == 0:
            print(f"오류: 사용자 '{username}'을 찾을 수 없습니다.")
            return False
        
        # 사용자 비활성화
        session.execute(
            text("UPDATE users SET is_active = 0 WHERE username = :username"),
            {"username": username}
        )
        
        # 사용자 공개키 비활성화
        session.execute(
            text("UPDATE user_keys SET is_active = 0 WHERE username = :username"),
            {"username": username}
        )
        
        session.commit()
        
        print(f"사용자 '{username}'이 성공적으로 비활성화되었습니다.")
        return True
        
    except Exception as e:
        print(f"오류: 사용자 삭제 실패 - {str(e)}")
        return False
    finally:
        session.close()

def main():
    parser = argparse.ArgumentParser(description="ContainerSSH 사용자 관리")
    subparsers = parser.add_subparsers(dest='command', help='사용 가능한 명령어')
    
    # add-user 명령어
    add_user_parser = subparsers.add_parser('add-user', help='새 사용자 추가')
    add_user_parser.add_argument('--username', required=True, help='사용자명')
    add_user_parser.add_argument('--password', required=True, help='패스워드')
    
    # list-users 명령어
    subparsers.add_parser('list-users', help='사용자 목록 조회')
    
    # add-key 명령어
    add_key_parser = subparsers.add_parser('add-key', help='공개키 추가')
    add_key_parser.add_argument('--username', required=True, help='사용자명')
    add_key_parser.add_argument('--key', required=True, help='공개키')
    add_key_parser.add_argument('--name', help='키 이름')
    
    # list-keys 명령어
    list_keys_parser = subparsers.add_parser('list-keys', help='공개키 목록 조회')
    list_keys_parser.add_argument('--username', required=True, help='사용자명')
    
    # delete-user 명령어
    delete_user_parser = subparsers.add_parser('delete-user', help='사용자 삭제')
    delete_user_parser.add_argument('--username', required=True, help='사용자명')
    
    args = parser.parse_args()
    
    if not args.command:
        parser.print_help()
        return
    
    if args.command == 'add-user':
        add_user(args.username, args.password)
    elif args.command == 'list-users':
        list_users()
    elif args.command == 'add-key':
        add_key(args.username, args.key, args.name)
    elif args.command == 'list-keys':
        list_keys(args.username)
    elif args.command == 'delete-user':
        delete_user(args.username)

if __name__ == '__main__':
    main()
