"""Streaming CSV import. The file is read in batches in a worker thread (never fully in
memory), rows are validated, and good rows are bulk-inserted with
ON CONFLICT (user_id, import_ref) DO NOTHING where import_ref = "<file sha256>:<row no>",
so re-running the same file (or resuming a crashed job) never duplicates expenses."""
import csv
import itertools
import os
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

import anyio
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app import db
from app.config import settings
from app.models import Category, Expense, ImportJob, OutboxEvent
from app.schemas import SUPPORTED_CURRENCIES

MAX_STORED_ERRORS = 1000


def validate_row(row: dict, categories: dict[str, int]) -> tuple[dict | None, str | None]:
    try:
        spent_on = date.fromisoformat((row.get("date") or "").strip())
    except ValueError:
        return None, "invalid date (expected YYYY-MM-DD)"
    try:
        amount = Decimal((row.get("amount") or "").strip())
    except InvalidOperation:
        return None, "invalid amount"
    if not amount.is_finite() or amount <= 0:
        return None, "amount must be greater than 0"
    if amount.as_tuple().exponent < -2:
        return None, "amount has more than 2 decimal places"
    if amount >= Decimal("1e12"):
        return None, "amount too large"
    currency = (row.get("currency") or "").strip().upper()
    if currency not in SUPPORTED_CURRENCIES:
        return None, "unsupported currency"
    category_id = categories.get((row.get("category") or "").strip().lower())
    if category_id is None:
        return None, "unknown category"
    return {"category_id": category_id, "amount": amount, "currency": currency,
            "description": (row.get("description") or "").strip()[:500], "spent_on": spent_on}, None


async def process_import(job_id: uuid.UUID, user_id: int, path: str) -> None:
    try:
        async with db.SessionLocal() as session:
            job = await session.get(ImportJob, job_id)
            job.status = "processing"
            await session.commit()
            cats = await session.execute(select(Category.id, Category.name, Category.monthly_budget)
                                         .where(Category.user_id == user_id))
            categories, budgeted = {}, set()
            for cid, name, budget in cats:
                categories[name.lower()] = cid
                if budget is not None:
                    budgeted.add(cid)

            fh = open(path, newline="", encoding="utf-8-sig")
            try:
                reader = enumerate(csv.DictReader(fh), start=2)  # row 1 is the header
                while True:
                    batch = await anyio.to_thread.run_sync(
                        lambda: list(itertools.islice(reader, settings.import_batch_size)))
                    if not batch:
                        break
                    good, errors, touched = [], [], set()
                    for row_no, row in batch:
                        if row_no - 1 > settings.import_max_rows:
                            errors.append({"row": row_no, "reason": "row limit exceeded"})
                            continue
                        clean, reason = validate_row(row, categories)
                        if reason:
                            errors.append({"row": row_no, "reason": reason})
                            continue
                        clean.update(user_id=user_id, import_ref=f"{job.file_hash}:{row_no}")
                        good.append(clean)
                        if clean["category_id"] in budgeted:
                            touched.add((clean["category_id"], clean["spent_on"].year, clean["spent_on"].month))
                    if good:
                        await session.execute(pg_insert(Expense).values(good)
                                              .on_conflict_do_nothing(index_elements=["user_id", "import_ref"]))
                    for cid, y, m in touched:
                        session.add(OutboxEvent(event_type="budget.check", payload={
                            "user_id": user_id, "category_id": cid, "year": y, "month": m}))
                    job.rows_done += len(good)
                    job.rows_failed += len(errors)
                    room = MAX_STORED_ERRORS - len(job.errors)
                    if room > 0 and errors:
                        job.errors = job.errors + errors[:room]  # reassign so JSONB change is tracked
                    await session.commit()  # one transaction per batch; API stays responsive
            finally:
                fh.close()
            job.status = "completed"
            job.finished_at = datetime.now(UTC)
            await session.commit()
    except Exception:
        async with db.SessionLocal() as session:
            job = await session.get(ImportJob, job_id)
            if job is not None:
                job.status = "failed"
                job.finished_at = datetime.now(UTC)
                await session.commit()
        raise
    finally:
        if os.path.exists(path):
            os.remove(path)
