import os
from typing import Dict, List

class Config:
    # 환경변수에서 설정 로드
    ALLOWED_USERS: Dict[str, str] = {
        # username: password_hash (bcrypt)
        "admin": "$2b$12$EixZaYVK1fsbw1ZfbX3OXePaWxn96p36WQoeG6Lruj3vjPGga31lW",  # secret
        "user1": "$2b$12$EixZaYVK1fsbw1ZfbX3OXePaWxn96p36WQoeG6Lruj3vjPGga31lW",  # secret
    }
    
    # SSH 공개키 (authorized_keys 형식)
    ALLOWED_KEYS: Dict[str, List[str]] = {
        "admin": [
            "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC..."  # 실제 공개키로 교체
        ],
        "user1": [
            "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQD..."  # 실제 공개키로 교체
        ]
    }
    
    @classmethod
    def load_from_env(cls):
        # Kubernetes ConfigMap이나 Secret에서 설정 로드
        pass
