from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.errors import AppError
from app.models import User
from app.security import decode_token

_bearer = HTTPBearer(auto_error=False)


async def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(get_session),
) -> User:
    if creds is None:
        raise AppError(401, "unauthorized", "Missing bearer token")
    claims = decode_token(creds.credentials, "access")
    if claims is None:
        raise AppError(401, "invalid_token", "Invalid or expired token")
    user = await session.get(User, int(claims["sub"]))
    # token_version mismatch = all tokens revoked (refresh-token reuse detected).
    if user is None or user.token_version != claims.get("tv"):
        raise AppError(401, "invalid_token", "Token revoked")
    return user
