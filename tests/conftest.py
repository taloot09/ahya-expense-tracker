import asyncio
import os
from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app import db, redis_client
from app.main import app
from app.models import Base

TEST_DB = os.getenv("TEST_DATABASE_URL", "postgresql+asyncpg://expense:expense@localhost:5432/expense_test")
TEST_REDIS = os.getenv("TEST_REDIS_URL", "redis://localhost:6379/15")

# Fixed rates: 1 USD = 250 PKR, 1 AED = 0.25 USD, 1 EUR = 1.10 USD
RATES = {"USD": Decimal("1"), "PKR": Decimal("0.004"), "AED": Decimal("0.25"), "EUR": Decimal("1.10")}
RATE_START, RATE_END = date(2026, 6, 1), date(2027, 1, 31)


def pytest_sessionstart(session):
    async def _setup():
        engine = create_async_engine(TEST_DB, poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        await engine.dispose()
    asyncio.run(_setup())


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(TEST_DB, poolclass=NullPool)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture(autouse=True)
async def clean_state(engine, monkeypatch):
    """Every test starts from empty tables, fresh rates and an empty Redis DB."""
    tables = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
        rows, d = [], RATE_START
        while d <= RATE_END:
            rows += [{"c": c, "r": r, "d": d} for c, r in RATES.items()]
            d += timedelta(days=1)
        await conn.execute(text("INSERT INTO exchange_rates (currency, rate_to_usd, valid_on) "
                                "VALUES (:c, :r, :d)"), rows)
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker)
    redis = Redis.from_url(TEST_REDIS, decode_responses=True)
    await redis.flushdb()
    monkeypatch.setattr(redis_client, "_client", redis)
    yield sessionmaker
    await redis.aclose()


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test/api/v1") as c:
        yield c


async def register_and_login(client, email="alice@example.com", base_currency="PKR", password="s3cret-pass"):
    r = await client.post("/auth/register", json={"email": email, "password": password,
                                                  "base_currency": base_currency})
    assert r.status_code == 201, r.text
    r = await client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


@pytest_asyncio.fixture
async def auth(client):
    tokens = await register_and_login(client)
    return {"Authorization": f"Bearer {tokens['access_token']}"}


@pytest_asyncio.fixture
async def other_auth(client):
    tokens = await register_and_login(client, email="bob@example.com", base_currency="AED")
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def make_category(client, headers, name="Food", budget=None):
    body = {"name": name}
    if budget is not None:
        body["monthly_budget"] = str(budget)
    r = await client.post("/categories", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def make_expense(client, headers, category_id, amount="100.00", currency="PKR",
                       spent_on="2026-10-10", description="lunch", extra_headers=None):
    r = await client.post("/expenses", headers={**headers, **(extra_headers or {})}, json={
        "category_id": category_id, "amount": amount, "currency": currency,
        "description": description, "spent_on": spent_on})
    assert r.status_code == 201, r.text
    return r.json()
