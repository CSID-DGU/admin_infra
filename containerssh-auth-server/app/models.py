from pydantic import BaseModel, Extra
from typing import Optional
from datetime import datetime

# ===== 인증 관련 모델 =====
class PasswordAuthRequest(BaseModel):
    username: str
    remoteAddress: str
    connectionId: str
    passwordBase64: str

class PublicKeyAuthRequest(BaseModel):
    username: str
    remoteAddress: str
    connectionId: str
    publicKey: str

class AuthResponse(BaseModel):
    success: bool
    authenticatedUsername: Optional[str] = None
    

    class Config:
        extra = Extra.forbid

# ===== 사용자 관리 모델 =====
class UserCreateRequest(BaseModel):
    username: str
    password: str
    
    class Config:
        schema_extra = {
            "example": {
                "username": "newuser",
                "password": "securepassword123"
            }
        }

class UserResponse(BaseModel):
    id: int
    username: str
    is_active: bool
    created_at: datetime
    
    class Config:
        schema_extra = {
            "example": {
                "id": 1,
                "username": "admin",
                "is_active": True,
                "created_at": "2024-01-01T12:00:00"
            }
        }

class UserKeyCreateRequest(BaseModel):
    public_key: str
    key_name: Optional[str] = None
    
    class Config:
        schema_extra = {
            "example": {
                "public_key": "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC7vbqajDhj...",
                "key_name": "laptop-key"
            }
        }

class UserKeyResponse(BaseModel):
    id: int
    username: str
    public_key: str
    key_name: Optional[str]
    is_active: bool
    created_at: datetime
    
    class Config:
        schema_extra = {
            "example": {
                "id": 1,
                "username": "admin",
                "public_key": "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC7vbqajDhj...",
                "key_name": "laptop-key",
                "is_active": True,
                "created_at": "2024-01-01T12:00:00"
            }
        }
