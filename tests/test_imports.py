from sqlalchemy import func, select

from app.models import Expense
from tests.conftest import make_category

CSV = (
    "date,amount,currency,category,description\n"
    "2026-10-01,100.00,PKR,Food,ok 1\n"
    "2026-10-02,5.5,usd,food,ok 2\n"
    "not-a-date,10,PKR,Food,bad date\n"
    "2026-10-03,-1,PKR,Food,negative\n"
    "2026-10-03,1.234,PKR,Food,3dp\n"
    "2026-10-03,10,XYZ,Food,bad currency\n"
    "2026-10-03,10,PKR,Nope,bad category\n"
    "2026-10-04,20,AED,Food,ok 3\n"
)


async def _upload(client, auth, content=CSV):
    r = await client.post("/expenses/import", headers=auth,
                          files={"file": ("expenses.csv", content.encode(), "text/csv")})
    assert r.status_code == 202, r.text
    return r.json()


async def test_import_counts_good_and_bad_rows(client, auth):
    await make_category(client, auth, "Food", "1000")
    job = await _upload(client, auth)
    r = await client.get(f"/imports/{job['job_id']}", headers=auth)
    body = r.json()
    assert body["status"] == "completed"
    assert body["rows_done"] == 3 and body["rows_failed"] == 5
    assert {e["row"] for e in body["errors"]} == {4, 5, 6, 7, 8}


async def test_same_file_twice_no_duplicates(client, auth, clean_state):
    await make_category(client, auth, "Food")
    first = await _upload(client, auth)
    second = await _upload(client, auth)
    assert second["job_id"] == first["job_id"] and second["duplicate"] is True
    async with clean_state() as s:
        assert await s.scalar(select(func.count()).select_from(Expense)) == 3


async def test_import_status_of_other_user_404(client, auth, other_auth):
    await make_category(client, auth, "Food")
    job = await _upload(client, auth)
    assert (await client.get(f"/imports/{job['job_id']}", headers=other_auth)).status_code == 404
