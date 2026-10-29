import asyncio

from sqlalchemy import func, select, update

from app.models import Expense, IdempotencyKey
from tests.conftest import make_category

BODY = {"amount": "42.00", "currency": "PKR", "description": "coffee", "spent_on": "2026-10-10"}


async def _count(sessionmaker):
    async with sessionmaker() as s:
        return await s.scalar(select(func.count()).select_from(Expense))


async def test_same_key_same_body_replays(client, auth, clean_state):
    cid = await make_category(client, auth)
    h = {**auth, "Idempotency-Key": "abc-1"}
    r1 = await client.post("/expenses", headers=h, json={**BODY, "category_id": cid})
    r2 = await client.post("/expenses", headers=h, json={**BODY, "category_id": cid})
    assert r1.status_code == r2.status_code == 201
    assert r1.json() == r2.json() and r2.headers["Idempotent-Replayed"] == "true"
    assert await _count(clean_state) == 1


async def test_same_key_different_body_422(client, auth):
    cid = await make_category(client, auth)
    h = {**auth, "Idempotency-Key": "abc-2"}
    await client.post("/expenses", headers=h, json={**BODY, "category_id": cid})
    r = await client.post("/expenses", headers=h, json={**BODY, "category_id": cid, "amount": "43.00"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "idempotency_key_mismatch"


async def test_parallel_same_key_creates_one_expense(client, auth, clean_state):
    cid = await make_category(client, auth)
    h = {**auth, "Idempotency-Key": "race-key"}
    responses = await asyncio.gather(*[
        client.post("/expenses", headers=h, json={**BODY, "category_id": cid}) for _ in range(5)])
    assert all(r.status_code == 201 for r in responses)
    assert len({r.json()["id"] for r in responses}) == 1
    assert await _count(clean_state) == 1


async def test_expired_key_is_treated_as_new(client, auth, clean_state):
    cid = await make_category(client, auth)
    h = {**auth, "Idempotency-Key": "old-key"}
    await client.post("/expenses", headers=h, json={**BODY, "category_id": cid})
    async with clean_state() as s:
        await s.execute(update(IdempotencyKey).values(expires_at=func.now() - func.make_interval(0, 0, 0, 1)))
        await s.commit()
    r = await client.post("/expenses", headers=h, json={**BODY, "category_id": cid})
    assert r.status_code == 201 and "Idempotent-Replayed" not in r.headers
    assert await _count(clean_state) == 2


async def test_failed_request_does_not_burn_key(client, auth, clean_state):
    h = {**auth, "Idempotency-Key": "k404"}
    r = await client.post("/expenses", headers=h, json={**BODY, "category_id": 999})
    assert r.status_code == 404
    cid = await make_category(client, auth)
    r = await client.post("/expenses", headers=h, json={**BODY, "category_id": cid})
    # different body hash than the failed attempt, but the failed attempt stored nothing
    assert r.status_code == 201
