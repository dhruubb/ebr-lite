"""Password hashing (bcrypt) and session tokens (JWT).

Note: bcrypt is used directly instead of passlib, because passlib is unmaintained and breaks
with bcrypt >= 4.1.
"""
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from . import config


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=config.BCRYPT_ROUNDS)).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


def create_token(user_id: int, role: str) -> str:
    # URS: session timeout. The token expires SESSION_MINUTES after login (real wall-clock time).
    now = datetime.now(timezone.utc)
    payload = {"sub": str(user_id), "role": role, "iat": now, "exp": now + timedelta(minutes=config.SESSION_MINUTES)}
    return jwt.encode(payload, config.JWT_SECRET, algorithm=config.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    return jwt.decode(token, config.JWT_SECRET, algorithms=[config.JWT_ALGORITHM])
