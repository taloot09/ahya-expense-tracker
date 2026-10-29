from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import OutboxEvent


def add_budget_check(session: AsyncSession, user_id: int, category_id: int, day: date) -> None:
    """Called inside the same transaction as the expense write (transactional outbox)."""
    session.add(OutboxEvent(event_type="budget.check", payload={
        "user_id": user_id, "category_id": category_id, "year": day.year, "month": day.month}))


def add_receipt_delete(session: AsyncSession, key: str) -> None:
    session.add(OutboxEvent(event_type="receipt.delete", payload={"key": key}))
