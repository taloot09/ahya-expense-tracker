import uuid
from datetime import UTC, datetime, timedelta

import anyio
import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from app.config import settings

_ph = PasswordHasher()


async def hash_password(password: str) -> str:
    # argon2 is CPU-bound by design; run it off the event loop.
    return await anyio.to_thread.run_sync(_ph.hash, password)


async def verify_password(hashed: str, password: str) -> bool:
    def _verify() -> bool:
        try:
            return _ph.verify(hashed, password)
        except (VerifyMismatchError, InvalidHashError):
            return False
    return await anyio.to_thread.run_sync(_verify)


def _encode(claims: dict) -> str:
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_access_token(user_id: int, token_version: int) -> str:
    now = datetime.now(UTC)
    return _encode({"sub": str(user_id), "type": "access", "tv": token_version,
                    "iat": now, "exp": now + timedelta(minutes=settings.access_token_minutes)})


def create_refresh_token(user_id: int) -> tuple[str, uuid.UUID, datetime]:
    now = datetime.now(UTC)
    jti = uuid.uuid4()
    exp = now + timedelta(days=settings.refresh_token_days)
    token = _encode({"sub": str(user_id), "type": "refresh", "jti": str(jti), "iat": now, "exp": exp})
    return token, jti, exp


def decode_token(token: str, expected_type: str) -> dict | None:
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    except jwt.PyJWTError:
        return None
    return claims if claims.get("type") == expected_type else None
