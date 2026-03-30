"""Simple email/password auth with JWT tokens."""
from __future__ import annotations
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt
from passlib.context import CryptContext
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import User

_pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
_bearer = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Password helpers
# ---------------------------------------------------------------------------

def hash_password(plain: str) -> str:
    return _pwd_ctx.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd_ctx.verify(plain, hashed)


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------

def create_access_token(user_id: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=settings.JWT_EXPIRE_MINUTES)
    payload = {"sub": user_id, "exp": expire}
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def _decode_token(token: str) -> str:
    """Return user_id from a valid token, raise 401 otherwise."""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
        user_id: str | None = payload.get("sub")
        if not user_id:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
        return user_id
    except JWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")


# ---------------------------------------------------------------------------
# FastAPI dependency: optional auth (returns user_id or None)
# ---------------------------------------------------------------------------

def get_optional_user_id(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str | None:
    if creds is None:
        return None
    return _decode_token(creds.credentials)


# ---------------------------------------------------------------------------
# FastAPI dependency: required auth
# ---------------------------------------------------------------------------

def get_current_user_id(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> str:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    return _decode_token(creds.credentials)


def get_current_user(
    user_id: str = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> User:
    user = db.get(User, user_id)
    if not user or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    return user


# ---------------------------------------------------------------------------
# CRUD helpers
# ---------------------------------------------------------------------------

def create_user(db: Session, email: str, plain_password: str, full_name: str | None = None) -> User:
    existing = db.query(User).filter(User.email == email).first()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Email already registered")
    user = User(
        id=str(uuid.uuid4()),
        email=email,
        hashed_password=hash_password(plain_password),
        full_name=full_name,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def authenticate_user(db: Session, email: str, plain_password: str) -> User:
    user = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(plain_password, user.hashed_password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="Incorrect email or password")
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Account is disabled")
    return user


# ---------------------------------------------------------------------------
# Document ownership check
# ---------------------------------------------------------------------------

def assert_owns_document(doc_user_id: str, current_user_id: str) -> None:
    """Raise 403 if current_user does not own the document."""
    if doc_user_id != current_user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="Access denied")
