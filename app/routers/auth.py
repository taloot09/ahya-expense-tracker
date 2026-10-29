import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import ratelimit
from app.db import get_session
from app.errors import AppError
from app.models import RefreshToken, User
from app.redis_client import get_redis
from app.schemas import LoginIn, RefreshIn, RegisterIn, TokenPair, UserOut
from app.security import (create_access_token, create_refresh_token, decode_token,
                          hash_password, verify_password)

router = APIRouter(prefix="/auth", tags=["auth"])


async def _issue_pair(session: AsyncSession, user: User) -> TokenPair:
    refresh, jti, exp = create_refresh_token(user.id)
    session.add(RefreshToken(jti=jti, user_id=user.id, expires_at=exp))
    return TokenPair(access_token=create_access_token(user.id, user.token_version), refresh_token=refresh)


@router.post("/register", status_code=201, response_model=UserOut)
async def register(body: RegisterIn, session: AsyncSession = Depends(get_session)):
    user = User(email=body.email.lower(), password_hash=await hash_password(body.password),
                base_currency=body.base_currency)
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        raise AppError(409, "email_taken", "Email already registered")
    await session.refresh(user)
    return user


@router.post("/login", response_model=TokenPair)
async def login(body: LoginIn, session: AsyncSession = Depends(get_session)):
    redis = get_redis()
    email = body.email.lower()
    await ratelimit.ensure_login_allowed(redis, email)
    user = await session.scalar(select(User).where(User.email == email))
    if user is None or not await verify_password(user.password_hash, body.password):
        await ratelimit.record_login_failure(redis, email)
        raise AppError(401, "invalid_credentials", "Invalid email or password")
    pair = await _issue_pair(session, user)
    await session.commit()
    return pair


@router.post("/refresh", response_model=TokenPair)
async def refresh(body: RefreshIn, session: AsyncSession = Depends(get_session)):
    claims = decode_token(body.refresh_token, "refresh")
    if claims is None:
        raise AppError(401, "invalid_token", "Invalid or expired refresh token")
    jti = uuid.UUID(claims["jti"])
    now = datetime.now(UTC)
    # Atomic "use once": only one caller can flip revoked_at from NULL.
    user_id = await session.scalar(
        update(RefreshToken)
        .where(RefreshToken.jti == jti, RefreshToken.revoked_at.is_(None), RefreshToken.expires_at > now)
        .values(revoked_at=now).returning(RefreshToken.user_id))
    if user_id is None:
        known = await session.get(RefreshToken, jti)
        if known is not None:
            # Token was already used/revoked -> it leaked. Kill every token of this user.
            await session.execute(update(RefreshToken)
                                  .where(RefreshToken.user_id == known.user_id, RefreshToken.revoked_at.is_(None))
                                  .values(revoked_at=now))
            await session.execute(update(User).where(User.id == known.user_id)
                                  .values(token_version=User.token_version + 1))
            await session.commit()
            raise AppError(401, "token_reused", "Refresh token reuse detected; all sessions revoked")
        raise AppError(401, "invalid_token", "Unknown refresh token")
    user = await session.get(User, user_id)
    pair = await _issue_pair(session, user)
    await session.commit()
    return pair


@router.post("/logout", status_code=204)
async def logout(body: RefreshIn, session: AsyncSession = Depends(get_session)):
    claims = decode_token(body.refresh_token, "refresh")
    if claims is None:
        raise AppError(401, "invalid_token", "Invalid or expired refresh token")
    await session.execute(update(RefreshToken)
                          .where(RefreshToken.jti == uuid.UUID(claims["jti"]), RefreshToken.revoked_at.is_(None))
                          .values(revoked_at=datetime.now(UTC)))
    await session.commit()
    return Response(status_code=204)
