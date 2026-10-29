import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import storage
from app.config import settings
from app.db import get_session
from app.deps import current_user
from app.errors import AppError, not_found
from app.models import Expense, User
from app.schemas import ConfirmIn, UploadUrlIn
from app.services import outbox

router = APIRouter(prefix="/expenses/{expense_id}/receipt", tags=["receipts"])


async def _expense(session: AsyncSession, user_id: int, expense_id: int) -> Expense:
    e = await session.scalar(select(Expense).where(Expense.id == expense_id, Expense.user_id == user_id))
    if e is None:
        raise not_found("expense")
    return e


@router.post("/upload-url")
async def upload_url(expense_id: int, body: UploadUrlIn, user: User = Depends(current_user),
                     session: AsyncSession = Depends(get_session)):
    await _expense(session, user.id, expense_id)
    ext = storage.CONTENT_TYPES.get(body.content_type)
    if ext is None:
        raise AppError(422, "unsupported_type", "Only image/jpeg, image/png and application/pdf are allowed")
    key = f"receipts/{user.id}/{expense_id}/{uuid.uuid4()}.{ext}"
    post = await storage.presigned_upload(key, body.content_type)
    return {"url": post["url"], "fields": post["fields"], "key": key,
            "max_bytes": settings.receipt_max_bytes, "expires_in": 300}


@router.post("/confirm")
async def confirm(expense_id: int, body: ConfirmIn, user: User = Depends(current_user),
                  session: AsyncSession = Depends(get_session)):
    expense = await _expense(session, user.id, expense_id)
    prefix = f"receipts/{user.id}/{expense_id}/"
    if not body.key.startswith(prefix) or "/" in body.key[len(prefix):]:
        raise AppError(422, "invalid_key", "Key does not belong to this expense")
    size = await storage.object_size(body.key)
    if size is None:
        raise AppError(422, "receipt_not_found", "File was not uploaded")
    real = storage.detect_type(await storage.read_first_bytes(body.key))
    claimed = body.key.rsplit(".", 1)[-1]
    if size > settings.receipt_max_bytes or real is None or real != claimed:
        await storage.delete_object(body.key)
        raise AppError(422, "invalid_file", "File is too large or its content is not a real JPG/PNG/PDF")
    old_key = expense.receipt_key
    await session.execute(update(Expense).where(Expense.id == expense_id, Expense.user_id == user.id)
                          .values(receipt_key=body.key))
    if old_key and old_key != body.key:
        outbox.add_receipt_delete(session, old_key)
    await session.commit()
    return {"receipt_key": body.key}


@router.get("")
async def download(expense_id: int, user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    expense = await _expense(session, user.id, expense_id)
    if not expense.receipt_key:
        raise not_found("receipt")
    return {"url": await storage.presigned_download(expense.receipt_key, 900), "expires_in": 900}
