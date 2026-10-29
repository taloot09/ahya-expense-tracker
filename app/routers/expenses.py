import base64
import binascii
import json
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import delete, func, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import current_user
from app.errors import AppError, not_found
from app.models import Expense, User
from app.schemas import Currency, ExpenseIn, ExpenseOut, ExpensePage, ExpensePatch
from app.services import outbox
from app.services.expenses import create_expense, owned_category

router = APIRouter(prefix="/expenses", tags=["expenses"])


def _etag(version: int) -> str:
    return f'"{version}"'


def _encode_cursor(e: Expense) -> str:
    raw = json.dumps({"d": e.spent_on.isoformat(), "i": e.id}).encode()
    return base64.urlsafe_b64encode(raw).decode()


def _decode_cursor(cursor: str) -> tuple[date, int]:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return date.fromisoformat(data["d"]), int(data["i"])
    except (binascii.Error, ValueError, KeyError, TypeError):
        raise AppError(400, "invalid_cursor", "Cursor is malformed")


@router.post("", status_code=201, response_model=ExpenseOut)
async def create(body: ExpenseIn, user: User = Depends(current_user), session: AsyncSession = Depends(get_session),
                 idempotency_key: str | None = Header(default=None, max_length=255)):
    status, payload, replayed = await create_expense(session, user.id, body, idempotency_key)
    headers = {"ETag": _etag(payload["version"])}
    if replayed:
        headers["Idempotent-Replayed"] = "true"
    return JSONResponse(payload, status_code=status, headers=headers)


@router.get("", response_model=ExpensePage)
async def list_expenses(
    user: User = Depends(current_user), session: AsyncSession = Depends(get_session),
    category_id: int | None = None, date_from: date | None = None, date_to: date | None = None,
    min_amount: Decimal | None = None, max_amount: Decimal | None = None,
    currency: Currency | None = None, q: str | None = Query(default=None, max_length=100),
    cursor: str | None = None, limit: int = Query(default=20, ge=1, le=100),
):
    stmt = select(Expense).where(Expense.user_id == user.id)
    if category_id is not None:
        stmt = stmt.where(Expense.category_id == category_id)
    if date_from is not None:
        stmt = stmt.where(Expense.spent_on >= date_from)
    if date_to is not None:
        stmt = stmt.where(Expense.spent_on <= date_to)
    if min_amount is not None:
        stmt = stmt.where(Expense.amount >= min_amount)
    if max_amount is not None:
        stmt = stmt.where(Expense.amount <= max_amount)
    if currency is not None:
        stmt = stmt.where(Expense.currency == currency)
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(Expense.description.ilike(f"%{escaped}%", escape="\\"))
    if cursor:
        # Keyset: strictly "after" the last row seen in (spent_on DESC, id DESC) order.
        # New rows inserted meanwhile can't shift pages the way OFFSET does.
        d, i = _decode_cursor(cursor)
        stmt = stmt.where(tuple_(Expense.spent_on, Expense.id) < tuple_(d, i))
    stmt = stmt.order_by(Expense.spent_on.desc(), Expense.id.desc()).limit(limit + 1)
    rows = (await session.scalars(stmt)).all()
    next_cursor = _encode_cursor(rows[limit - 1]) if len(rows) > limit else None
    return ExpensePage(items=rows[:limit], next_cursor=next_cursor)


async def _owned_expense(session: AsyncSession, user_id: int, expense_id: int) -> Expense:
    e = await session.scalar(select(Expense).where(Expense.id == expense_id, Expense.user_id == user_id))
    if e is None:
        raise not_found("expense")  # 404 even if it exists for someone else
    return e


@router.get("/{expense_id}", response_model=ExpenseOut)
async def get_one(expense_id: int, response: Response, user: User = Depends(current_user),
                  session: AsyncSession = Depends(get_session)):
    e = await _owned_expense(session, user.id, expense_id)
    response.headers["ETag"] = _etag(e.version)
    return e


def _parse_if_match(value: str | None) -> int:
    if value is None:
        raise AppError(428, "precondition_required", "If-Match header is required")
    try:
        return int(value.strip().removeprefix("W/").strip('"'))
    except ValueError:
        raise AppError(412, "version_conflict", "If-Match does not match current version")


@router.patch("/{expense_id}", response_model=ExpenseOut)
async def patch(expense_id: int, body: ExpensePatch, request: Request, response: Response,
                user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    version = _parse_if_match(request.headers.get("if-match"))
    values = body.model_dump(exclude_unset=True)
    if not values:
        raise AppError(422, "validation_error", "No fields to update")
    nulls = [k for k, v in values.items() if v is None]
    if nulls:
        raise AppError(422, "validation_error", "Fields cannot be null",
                       details=[{"field": k, "message": "must not be null"} for k in nulls])
    if "category_id" in values:
        await owned_category(session, user.id, values["category_id"])
    # Version check + write in ONE statement: concurrent writers serialize on the row lock,
    # the loser re-evaluates "version = :v", matches 0 rows and gets 412.
    result = await session.execute(
        update(Expense.__table__)
        .where(Expense.id == expense_id, Expense.user_id == user.id, Expense.version == version)
        .values(**values, version=Expense.version + 1, updated_at=func.now())
        .returning(*Expense.__table__.c))
    updated = result.mappings().first()
    if updated is None:
        user_id = user.id  # read before rollback expires ORM objects
        await session.rollback()
        await _owned_expense(session, user_id, expense_id)  # 404 if not mine
        raise AppError(412, "version_conflict", "Expense was modified by someone else")
    # Totals can only go UP in the expense's (new) category/month, so that's the one to check.
    outbox.add_budget_check(session, user.id, updated["category_id"], updated["spent_on"])
    out = ExpenseOut.model_validate(dict(updated))
    await session.commit()
    response.headers["ETag"] = _etag(out.version)
    return out


@router.delete("/{expense_id}", status_code=204)
async def remove(expense_id: int, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    result = await session.execute(
        delete(Expense).where(Expense.id == expense_id, Expense.user_id == user.id)
        .returning(Expense.id, Expense.receipt_key))
    row = result.first()
    if row is None:
        raise not_found("expense")
    if row.receipt_key:
        # S3 delete happens in the outbox worker: committed with the DB delete, retried until done.
        outbox.add_receipt_delete(session, row.receipt_key)
    await session.commit()
    return Response(status_code=204)
