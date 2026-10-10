"""Shared FastAPI dependencies: DB session, current user, role guard."""
import jwt
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .db import get_db
from .errors import Forbidden, SessionInvalid
from .models import User
from .security import decode_token


bearer = HTTPBearer(auto_error=False)   # also gives the "Authorize" button in /docs


def current_user(creds: HTTPAuthorizationCredentials | None = Depends(bearer), db: Session = Depends(get_db)) -> User:
    if creds is None:
        raise SessionInvalid("Not logged in.")
    try:
        payload = decode_token(creds.credentials)
    except jwt.ExpiredSignatureError:
        raise SessionInvalid("Session expired. Please log in again.")
    except jwt.PyJWTError:
        raise SessionInvalid("Invalid session token.")
    user = db.get(User, int(payload["sub"]))
    if user is None or not user.active:
        raise SessionInvalid("User not found or inactive.")
    return user


def require_roles(*roles: str):
    """URS-009: role-based access on every endpoint."""
    def guard(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise Forbidden(f"Role {user.role} is not allowed here (requires {', '.join(roles)}).")
        return user
    return guard
