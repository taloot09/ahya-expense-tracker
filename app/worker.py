"""Outbox worker: `python -m app.worker`. Run as many copies as you like.

Each batch is claimed with SELECT ... FOR UPDATE SKIP LOCKED, so two workers never
handle the same event. Handlers are idempotent (notifications have a UNIQUE
constraint + ON CONFLICT DO NOTHING; S3 delete of a missing key is a no-op), so a
crash after the side effect but before marking processed is harmless."""
import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app import db, storage
from app.models import IdempotencyKey, Notification, OutboxEvent
from app.services.reports import monthly_report

log = logging.getLogger("worker")
LEVELS = (80, 100)
MAX_ATTEMPTS = 10


async def handle_budget_check(session: AsyncSession, p: dict) -> None:
    rows = await monthly_report(session, p["user_id"], p["year"], p["month"], p["category_id"])
    if not rows or rows[0]["percent_used"] is None:
        return
    row = rows[0]
    for level in LEVELS:
        if row["percent_used"] >= level:
            await session.execute(pg_insert(Notification).values(
                user_id=p["user_id"], category_id=p["category_id"], year=p["year"], month=p["month"],
                level=level,
                message=f"'{row['category']}' is at {row['percent_used']}% of its budget "
                        f"for {p['year']}-{p['month']:02d} (crossed {level}%)",
            ).on_conflict_do_nothing(constraint="uq_notification_once"))


async def handle_receipt_delete(session: AsyncSession, p: dict) -> None:
    await storage.delete_object(p["key"])


HANDLERS = {"budget.check": handle_budget_check, "receipt.delete": handle_receipt_delete}


async def process_batch(limit: int = 100) -> int:
    async with db.SessionLocal() as session, session.begin():
        events = (await session.scalars(
            select(OutboxEvent)
            .where(OutboxEvent.processed_at.is_(None), OutboxEvent.attempts < MAX_ATTEMPTS)
            .order_by(OutboxEvent.id).limit(limit).with_for_update(skip_locked=True))).all()
        for ev in events:
            try:
                async with session.begin_nested():
                    await HANDLERS[ev.event_type](session, ev.payload)
                ev.processed_at = datetime.now(UTC)
            except Exception as exc:  # keep the event, retry later
                ev.attempts += 1
                ev.last_error = str(exc)[:500]
                log.warning("event %s failed: %s", ev.id, exc)
        return len(events)


async def purge_expired_idempotency_keys() -> None:
    async with db.SessionLocal() as session, session.begin():
        await session.execute(delete(IdempotencyKey).where(IdempotencyKey.expires_at <= datetime.now(UTC)))


async def main() -> None:  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    ticks = 0
    while True:
        n = await process_batch()
        ticks += 1
        if ticks % 600 == 0:
            await purge_expired_idempotency_keys()
        if n == 0:
            await asyncio.sleep(1)


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
