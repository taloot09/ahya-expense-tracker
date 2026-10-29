import asyncio

import pytest

from tests.conftest import make_category, make_expense


@pytest.mark.parametrize("amount", ["0", "-5.00", "10.123"])
async def test_invalid_amounts_rejected(client, auth, amount):
    cid = await make_category(client, auth)
    r = await client.post("/expenses", headers=auth, json={
        "category_id": cid, "amount": amount, "currency": "PKR", "spent_on": "2026-10-01"})
    assert r.status_code == 422
    assert r.json()["error"]["details"][0]["field"].endswith("amount")


async def test_create_and_get_expense_with_etag(client, auth):
    cid = await make_category(client, auth)
    e = await make_expense(client, auth, cid, amount="99.99")
    r = await client.get(f"/expenses/{e['id']}", headers=auth)
    assert r.status_code == 200
    assert r.headers["ETag"] == '"1"' and r.json()["amount"] == "99.99"


async def test_other_users_expense_is_404(client, auth, other_auth):
    cid = await make_category(client, auth)
    e = await make_expense(client, auth, cid)
    assert (await client.get(f"/expenses/{e['id']}", headers=other_auth)).status_code == 404
    r = await client.patch(f"/expenses/{e['id']}", headers={**other_auth, "If-Match": '"1"'},
                           json={"amount": "1.00"})
    assert r.status_code == 404
    assert (await client.delete(f"/expenses/{e['id']}", headers=other_auth)).status_code == 404


async def test_filters(client, auth):
    food = await make_category(client, auth, "Food")
    travel = await make_category(client, auth, "Travel")
    await make_expense(client, auth, food, amount="10.00", currency="PKR", description="Biryani", spent_on="2026-10-01")
    await make_expense(client, auth, food, amount="500.00", currency="USD", description="Dinner", spent_on="2026-10-05")
    await make_expense(client, auth, travel, amount="50.00", currency="AED", description="Taxi", spent_on="2026-10-07")
    async def ids(params):
        r = await client.get("/expenses", headers=auth, params=params)
        assert r.status_code == 200, r.text
        return [i["description"] for i in r.json()["items"]]
    assert await ids({"category_id": travel}) == ["Taxi"]
    assert await ids({"currency": "usd"}) == ["Dinner"]
    assert await ids({"q": "biry"}) == ["Biryani"]
    assert await ids({"min_amount": "20", "max_amount": "100"}) == ["Taxi"]
    assert await ids({"date_from": "2026-10-02", "date_to": "2026-10-06"}) == ["Dinner"]
    assert await ids({"q": "%"}) == []


async def test_cursor_pagination_no_duplicates_while_inserting(client, auth):
    cid = await make_category(client, auth)
    original = set()
    for i in range(25):
        e = await make_expense(client, auth, cid, spent_on=f"2026-10-{(i % 10) + 1:02d}", description=f"e{i}")
        original.add(e["id"])
    seen, cursor, page = [], None, 0
    while True:
        params = {"limit": 7} | ({"cursor": cursor} if cursor else {})
        r = await client.get("/expenses", headers=auth, params=params)
        body = r.json()
        seen += [i["id"] for i in body["items"]]
        page += 1
        # New rows (same dates as existing ones and newer) arrive between page requests.
        await make_expense(client, auth, cid, spent_on="2026-10-20", description=f"new{page}")
        await make_expense(client, auth, cid, spent_on="2026-10-05", description=f"mid{page}")
        cursor = body["next_cursor"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)), "duplicate rows across pages"
    assert original <= set(seen), "rows went missing"


async def test_invalid_cursor_400(client, auth):
    r = await client.get("/expenses", headers=auth, params={"cursor": "!!!notbase64"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_cursor"


async def test_patch_requires_if_match_428(client, auth):
    cid = await make_category(client, auth)
    e = await make_expense(client, auth, cid)
    r = await client.patch(f"/expenses/{e['id']}", headers=auth, json={"amount": "5.00"})
    assert r.status_code == 428


async def test_patch_success_bumps_version(client, auth):
    cid = await make_category(client, auth)
    e = await make_expense(client, auth, cid)
    r = await client.patch(f"/expenses/{e['id']}", headers={**auth, "If-Match": '"1"'},
                           json={"amount": "5.50", "description": "edited"})
    assert r.status_code == 200
    assert r.headers["ETag"] == '"2"' and r.json()["amount"] == "5.50"
    r = await client.patch(f"/expenses/{e['id']}", headers={**auth, "If-Match": '"1"'}, json={"amount": "6.00"})
    assert r.status_code == 412


async def test_patch_rejects_empty_and_null(client, auth):
    cid = await make_category(client, auth)
    e = await make_expense(client, auth, cid)
    h = {**auth, "If-Match": '"1"'}
    assert (await client.patch(f"/expenses/{e['id']}", headers=h, json={})).status_code == 422
    assert (await client.patch(f"/expenses/{e['id']}", headers=h, json={"amount": None})).status_code == 422


async def test_parallel_patches_one_wins_one_412(client, auth):
    cid = await make_category(client, auth)
    e = await make_expense(client, auth, cid)
    h = {**auth, "If-Match": '"1"'}
    r1, r2 = await asyncio.gather(
        client.patch(f"/expenses/{e['id']}", headers=h, json={"amount": "1.00"}),
        client.patch(f"/expenses/{e['id']}", headers=h, json={"amount": "2.00"}))
    assert sorted([r1.status_code, r2.status_code]) == [200, 412]


async def test_delete_expense(client, auth):
    cid = await make_category(client, auth)
    e = await make_expense(client, auth, cid)
    assert (await client.delete(f"/expenses/{e['id']}", headers=auth)).status_code == 204
    assert (await client.get(f"/expenses/{e['id']}", headers=auth)).status_code == 404
