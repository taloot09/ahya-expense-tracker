import hashlib
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.errors import AppError, not_found
from app.models import Category, Expense, IdempotencyKey
from app.schemas import ExpenseIn, ExpenseOut
from app.services import outbox


async def owned_category(session: AsyncSession, user_id: int, category_id: int) -> Category:
    cat = await session.scalar(select(Category).where(Category.id == category_id, Category.user_id == user_id))
    if cat is None:
        raise not_found("category")
    return cat


def request_hash(body: ExpenseIn) -> str:
    canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


async def create_expense(session: AsyncSession, user_id: int, body: ExpenseIn,
                         idem_key: str | None) -> tuple[int, dict, bool]:
    """Returns (status, body, replayed).

    Idempotency race: the key row is INSERTed first, inside the same transaction as the
    expense. A concurrent request with the same key blocks on the primary-key index until
    the first transaction commits, then gets ON CONFLICT DO NOTHING and reads the stored
    response. If the first transaction rolls back, the second one simply proceeds.
    """
    if idem_key is not None:
        now = datetime.now(UTC)
        rhash = request_hash(body)
        # Expired keys behave as if they never existed.
        await session.execute(delete(IdempotencyKey).where(
            IdempotencyKey.user_id == user_id, IdempotencyKey.key == idem_key, IdempotencyKey.expires_at <= now))
        inserted = await session.scalar(
            pg_insert(IdempotencyKey)
            .values(user_id=user_id, key=idem_key, request_hash=rhash,
                    expires_at=now + timedelta(hours=settings.idempotency_ttl_hours))
            .on_conflict_do_nothing().returning(IdempotencyKey.key))
        if inserted is None:
            stored = (await session.execute(
                select(IdempotencyKey.request_hash, IdempotencyKey.status_code, IdempotencyKey.response_body)
                .where(IdempotencyKey.user_id == user_id, IdempotencyKey.key == idem_key))).one()
            await session.rollback()
            if stored.request_hash != rhash:
                raise AppError(422, "idempotency_key_mismatch",
                               "Idempotency-Key was already used with a different request body")
            return stored.status_code, stored.response_body, True

    await owned_category(session, user_id, body.category_id)
    expense = Expense(user_id=user_id, **body.model_dump())
    session.add(expense)
    await session.flush()
    outbox.add_budget_check(session, user_id, body.category_id, body.spent_on)
    out = ExpenseOut.model_validate(expense).model_dump(mode="json")
    if idem_key is not None:
        row = await session.get(IdempotencyKey, (user_id, idem_key))
        row.status_code, row.response_body = 201, out
    await session.commit()
    return 201, out, False
