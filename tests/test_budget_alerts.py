from sqlalchemy import func, select, update

from app import worker
from app.models import Notification, OutboxEvent
from tests.conftest import make_category, make_expense


async def _notifications(sessionmaker):
    async with sessionmaker() as s:
        return (await s.scalars(select(Notification).order_by(Notification.level))).all()


async def test_expense_writes_outbox_row_not_notification(client, auth, clean_state):
    cid = await make_category(client, auth, "Food", "100.00")
    await make_expense(client, auth, cid, amount="90.00")
    async with clean_state() as s:
        assert await s.scalar(select(func.count()).select_from(OutboxEvent)) == 1
    assert await _notifications(clean_state) == []


async def test_crossing_80_then_100(client, auth, clean_state):
    cid = await make_category(client, auth, "Food", "100.00")
    await make_expense(client, auth, cid, amount="85.00")
    await worker.process_batch()
    assert [n.level for n in await _notifications(clean_state)] == [80]
    await make_expense(client, auth, cid, amount="20.00")
    await worker.process_batch()
    assert [n.level for n in await _notifications(clean_state)] == [80, 100]
    r = await client.get("/notifications", headers=auth)
    assert r.status_code == 200 and len(r.json()) == 2


async def test_same_event_processed_twice_creates_one_notification(client, auth, clean_state):
    cid = await make_category(client, auth, "Food", "100.00")
    await make_expense(client, auth, cid, amount="100.00")
    await worker.process_batch()
    # Simulate a crash after the side effect but before the ack: event is redelivered.
    async with clean_state() as s:
        await s.execute(update(OutboxEvent).values(processed_at=None))
        await s.commit()
    await worker.process_batch()
    assert [n.level for n in await _notifications(clean_state)] == [80, 100]


async def test_failing_handler_is_retried_later(client, auth, clean_state, monkeypatch):
    async def boom(session, payload):
        raise RuntimeError("s3 down")
    monkeypatch.setitem(worker.HANDLERS, "receipt.delete", boom)
    async with clean_state() as s:
        s.add(OutboxEvent(event_type="receipt.delete", payload={"key": "x"}))
        await s.commit()
    await worker.process_batch()
    async with clean_state() as s:
        ev = await s.scalar(select(OutboxEvent))
        assert ev.processed_at is None and ev.attempts == 1 and "s3 down" in ev.last_error


async def test_purge_expired_idempotency_keys_runs(clean_state):
    await worker.purge_expired_idempotency_keys()
