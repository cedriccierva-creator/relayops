# Shared FastAPI dependency: get_current_user reads the JWT from the httpOnly cookie.
from fastapi import Cookie, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import User
from app.security import decode_access_token

COOKIE_NAME = "access_token"


def get_current_user(
    access_token: str | None = Cookie(default=None),
    db: Session = Depends(get_db),
) -> User:
    unauthorized = HTTPException(status_code=401, detail="Not authenticated")
    if not access_token:
        raise unauthorized
    user_id = decode_access_token(access_token)
    if user_id is None:
        raise unauthorized
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise unauthorized
    return user
