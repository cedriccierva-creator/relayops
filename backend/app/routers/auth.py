# Auth endpoints: register, login (sets the httpOnly cookie), logout, me.
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.deps import COOKIE_NAME, get_current_user
from app.models import User
from app.schemas import LoginRequest, UserCreate, UserOut
from app.security import DUMMY_HASH, create_access_token, hash_password, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/policy")
def policy():
    return {"registration_enabled": get_settings().REGISTRATION_ENABLED}


@router.post("/register", response_model=UserOut, status_code=201)
def register(payload: UserCreate, db: Session = Depends(get_db)):
    if not get_settings().REGISTRATION_ENABLED:
        raise HTTPException(status_code=403, detail="Registration is disabled. Contact the instance administrator.")
    if db.scalar(select(User).where(User.email == payload.email)):
        raise HTTPException(status_code=409, detail="Email already registered")
    user = User(email=payload.email, hashed_password=hash_password(payload.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:  # two requests raced past the check above
        db.rollback()
        raise HTTPException(status_code=409, detail="Email already registered")
    db.refresh(user)
    return user


@router.post("/login", response_model=UserOut)
def login(payload: LoginRequest, response: Response, db: Session = Depends(get_db)):
    settings = get_settings()
    user = db.scalar(select(User).where(User.email == payload.email))
    # Always verify a hash, even for unknown emails, so both failure paths cost the same time.
    password_ok = verify_password(payload.password, user.hashed_password if user else DUMMY_HASH)
    if user is None or not password_ok or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    response.set_cookie(
        key=COOKIE_NAME,
        value=create_access_token(user.id),
        httponly=True,
        samesite="lax",
        secure=settings.COOKIE_SECURE,
        path="/",
        max_age=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )
    return user


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(
        COOKIE_NAME, path="/", secure=get_settings().COOKIE_SECURE, httponly=True, samesite="lax"
    )
    return {"detail": "Logged out"}


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user
