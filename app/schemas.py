from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr, Field

SUPPORTED_CURRENCIES = {"PKR", "AED", "USD", "EUR"}


def _currency(v: str) -> str:
    v = v.upper()
    if v not in SUPPORTED_CURRENCIES:
        raise ValueError(f"currency must be one of {sorted(SUPPORTED_CURRENCIES)}")
    return v


Currency = Annotated[str, AfterValidator(_currency)]
# > 0, at most 2 decimal places. Decimal end to end, never float.
Amount = Annotated[Decimal, Field(gt=0, max_digits=14, decimal_places=2)]


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    base_currency: Currency


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    base_currency: str
    created_at: datetime


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class RefreshIn(BaseModel):
    refresh_token: str


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    monthly_budget: Amount | None = None


class CategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    monthly_budget: Decimal | None


class CategoryWithSpend(CategoryOut):
    spent_this_month: Decimal
    percent_used: Decimal | None


class ExpenseIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category_id: int
    amount: Amount
    currency: Currency
    description: str = Field(default="", max_length=500)
    spent_on: date


class ExpensePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category_id: int | None = None
    amount: Amount | None = None
    currency: Currency | None = None
    description: str | None = Field(default=None, max_length=500)
    spent_on: date | None = None


class ExpenseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    category_id: int
    amount: Decimal
    currency: str
    description: str
    spent_on: date
    receipt_key: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class ExpensePage(BaseModel):
    items: list[ExpenseOut]
    next_cursor: str | None


class ReportItem(BaseModel):
    category_id: int
    category: str
    budget: Decimal | None
    total: Decimal
    percent_used: Decimal | None
    previous_total: Decimal
    change_amount: Decimal
    change_percent: Decimal | None


class MonthlyReport(BaseModel):
    year: int
    month: int
    base_currency: str
    items: list[ReportItem]


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    category_id: int
    year: int
    month: int
    level: int
    message: str
    created_at: datetime


class UploadUrlIn(BaseModel):
    content_type: str


class ConfirmIn(BaseModel):
    key: str
