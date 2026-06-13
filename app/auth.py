from datetime import datetime, timedelta
from typing import Optional
from fastapi import Request, HTTPException, status
from jose import JWTError, jwt
import os
import hashlib

# Security configuration
SECRET_KEY = os.getenv("JWT_SECRET", "supplies-app-secret-key-change-this")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_DAYS = 30

class NotAuthenticatedException(Exception):
    pass

def hash_password(password: str) -> str:
    """Hash a password using SHA256"""
    return hashlib.sha256(password.encode('utf-8')).hexdigest()

def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verify a password against its SHA256 hash"""
    return hash_password(plain_password) == hashed_password

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    """Create JWT access token"""
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(days=ACCESS_TOKEN_EXPIRE_DAYS)
    
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

def get_current_user(request: Request):
    """Dependency to verify JWT token from cookie"""
    token = request.cookies.get("access_token")
    if not token:
        raise NotAuthenticatedException()
    
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise NotAuthenticatedException()
        return username
    except JWTError:
        raise NotAuthenticatedException()
