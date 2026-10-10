from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import current_user
from ..errors import AuthFailed
from ..models import User
from ..security import create_token, verify_password
from ..services import audit

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    username: str
    password: str


def user_out(u: User) -> dict:
    return {"id": u.id, "username": u.username, "full_name": u.full_name, "role": u.role}


@router.post("/login")
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.username == body.username))
    if user is None or not user.active or not verify_password(body.password, user.password_hash):
        audit.log(db, None, "LOGIN_FAILED", "user", body.username, username=body.username)
        db.commit()
        raise AuthFailed("Invalid user ID or password.")
    audit.log(db, user, "LOGIN", "user", user.id)
    db.commit()
    return {"access_token": create_token(user.id, user.role), "token_type": "bearer", "user": user_out(user)}


@router.get("/me")
def me(user: User = Depends(current_user)):
    return user_out(user)
