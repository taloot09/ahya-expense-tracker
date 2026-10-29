import hashlib
import os
import tempfile
import uuid

import anyio
from fastapi import APIRouter, BackgroundTasks, Depends, File, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import current_user
from app.errors import not_found
from app.models import ImportJob, User
from app.services.importer import process_import

router = APIRouter(tags=["imports"])
CHUNK = 1024 * 1024


@router.post("/expenses/import", status_code=202)
async def import_csv(background: BackgroundTasks, file: UploadFile = File(...),
                     user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    # Stream the upload to a temp file in 1 MB chunks while hashing it.
    fd, path = tempfile.mkstemp(suffix=".csv")
    os.close(fd)
    digest = hashlib.sha256()
    async with await anyio.open_file(path, "wb") as out:
        while chunk := await file.read(CHUNK):
            digest.update(chunk)
            await out.write(chunk)
    file_hash = digest.hexdigest()

    existing = await session.scalar(select(ImportJob).where(ImportJob.user_id == user.id,
                                                            ImportJob.file_hash == file_hash))
    if existing is None:
        job = ImportJob(id=uuid.uuid4(), user_id=user.id, file_hash=file_hash, status="pending",
                        rows_done=0, rows_failed=0, errors=[])
        session.add(job)
        try:
            await session.commit()
        except IntegrityError:  # same file uploaded twice at the same moment
            await session.rollback()
            existing = await session.scalar(select(ImportJob).where(ImportJob.user_id == user.id,
                                                                    ImportJob.file_hash == file_hash))
    if existing is not None:
        os.remove(path)
        return JSONResponse({"job_id": str(existing.id), "status": existing.status, "duplicate": True}, 202)
    background.add_task(process_import, job.id, user.id, path)
    return JSONResponse({"job_id": str(job.id), "status": "pending", "duplicate": False}, 202)


@router.get("/imports/{job_id}")
async def import_status(job_id: uuid.UUID, user: User = Depends(current_user),
                        session: AsyncSession = Depends(get_session)):
    job = await session.scalar(select(ImportJob).where(ImportJob.id == job_id, ImportJob.user_id == user.id))
    if job is None:
        raise not_found("import job")
    return {"job_id": str(job.id), "status": job.status, "rows_done": job.rows_done,
            "rows_failed": job.rows_failed, "errors": job.errors}
