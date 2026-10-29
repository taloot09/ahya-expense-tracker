from datetime import date

from tests.conftest import make_category, make_expense


async def test_create_and_duplicate_category(client, auth):
    await make_category(client, auth, "Food", "1000")
    r = await client.post("/categories", json={"name": "Food"}, headers=auth)
    assert r.status_code == 409


async def test_list_categories_with_this_month_spend(client, auth):
    cid = await make_category(client, auth, "Food", "1000.00")
    await make_category(client, auth, "Rent")
    today = date.today().isoformat()
    await make_expense(client, auth, cid, amount="250.00", currency="PKR", spent_on=today)
    r = await client.get("/categories", headers=auth)
    assert r.status_code == 200
    food = next(c for c in r.json() if c["name"] == "Food")
    assert food["spent_this_month"] == "250.00" and food["percent_used"] == "25.00"
    rent = next(c for c in r.json() if c["name"] == "Rent")
    assert rent["percent_used"] is None


async def test_cannot_use_other_users_category(client, auth, other_auth):
    cid = await make_category(client, auth)
    r = await client.post("/expenses", headers=other_auth, json={
        "category_id": cid, "amount": "1.00", "currency": "AED", "spent_on": "2026-10-01"})
    assert r.status_code == 404
