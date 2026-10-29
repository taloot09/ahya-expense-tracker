from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import current_user
from app.models import Notification, User
from app.schemas import MonthlyReport, NotificationOut
from app.services.reports import monthly_report

router = APIRouter(tags=["reports"])


@router.get("/reports/monthly", response_model=MonthlyReport)
async def report(year: int = Query(ge=2000, le=2100), month: int = Query(ge=1, le=12),
                 user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    items = await monthly_report(session, user.id, year, month)
    return MonthlyReport(year=year, month=month, base_currency=user.base_currency, items=items)


@router.get("/notifications", response_model=list[NotificationOut])
async def notifications(limit: int = Query(default=50, ge=1, le=200), user: User = Depends(current_user),
                        session: AsyncSession = Depends(get_session)):
    rows = await session.scalars(select(Notification).where(Notification.user_id == user.id)
                                 .order_by(Notification.id.desc()).limit(limit))
    return rows.all()
