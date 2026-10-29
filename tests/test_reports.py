from decimal import Decimal

from sqlalchemy import text

from tests.conftest import make_category, make_expense


async def test_monthly_report_mixed_currencies_and_missing_rate(client, auth, clean_state):
    # Base currency PKR (1 USD = 250 PKR). AED rate missing on 2026-10-10, so the
    # 2026-10-09 rate (changed to 0.30 USD) must be used.
    async with clean_state() as s:
        await s.execute(text("DELETE FROM exchange_rates WHERE currency='AED' AND valid_on='2026-10-10'"))
        await s.execute(text("UPDATE exchange_rates SET rate_to_usd=0.30 WHERE currency='AED' AND valid_on='2026-10-09'"))
        await s.commit()
    food = await make_category(client, auth, "Food", "20000.00")
    other = await make_category(client, auth, "Other")
    await make_expense(client, auth, food, amount="100.00", currency="AED", spent_on="2026-10-10")  # 7500
    await make_expense(client, auth, food, amount="10.00", currency="USD", spent_on="2026-10-05")   # 2500
    await make_expense(client, auth, food, amount="1000.00", currency="PKR", spent_on="2026-10-20")  # 1000
    await make_expense(client, auth, food, amount="5000.00", currency="PKR", spent_on="2026-09-15")  # last month
    await make_expense(client, auth, other, amount="1.00", currency="EUR", spent_on="2026-11-01")  # next month

    r = await client.get("/reports/monthly", headers=auth, params={"year": 2026, "month": 10})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["base_currency"] == "PKR"
    items = {i["category"]: i for i in body["items"]}
    f = items["Food"]
    assert Decimal(f["total"]) == Decimal("11000.00")
    assert Decimal(f["previous_total"]) == Decimal("5000.00")
    assert Decimal(f["change_amount"]) == Decimal("6000.00")
    assert Decimal(f["change_percent"]) == Decimal("120.00")
    assert Decimal(f["percent_used"]) == Decimal("55.00")
    assert Decimal(items["Other"]["total"]) == 0 and items["Other"]["change_percent"] is None


async def test_report_rejects_bad_month(client, auth):
    r = await client.get("/reports/monthly", headers=auth, params={"year": 2026, "month": 13})
    assert r.status_code == 422
