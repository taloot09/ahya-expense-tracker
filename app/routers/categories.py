from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.deps import current_user
from app.errors import AppError
from app.models import Category, User
from app.schemas import CategoryIn, CategoryOut, CategoryWithSpend
from app.services.reports import monthly_report

router = APIRouter(prefix="/categories", tags=["categories"])


@router.post("", status_code=201, response_model=CategoryOut)
async def create_category(body: CategoryIn, user: User = Depends(current_user),
                          session: AsyncSession = Depends(get_session)):
    cat = Category(user_id=user.id, name=body.name.strip(), monthly_budget=body.monthly_budget)
    session.add(cat)
    try:
        await session.commit()
    except IntegrityError:
        raise AppError(409, "duplicate_category", "Category with this name already exists")
    return cat


@router.get("", response_model=list[CategoryWithSpend])
async def list_categories(user: User = Depends(current_user), session: AsyncSession = Depends(get_session)):
    today = date.today()
    spend = {r["category_id"]: r for r in await monthly_report(session, user.id, today.year, today.month)}
    cats = (await session.scalars(select(Category).where(Category.user_id == user.id)
                                  .order_by(Category.name))).all()
    return [CategoryWithSpend(id=c.id, name=c.name, monthly_budget=c.monthly_budget,
                              spent_this_month=spend[c.id]["total"] if c.id in spend else Decimal("0.00"),
                              percent_used=spend[c.id]["percent_used"] if c.id in spend else None)
            for c in cats]
