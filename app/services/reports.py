from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# One SQL statement:
#  conv  - every expense of the current + previous month converted to base currency using
#          the latest rate ON OR BEFORE spent_on (LATERAL ... ORDER BY valid_on DESC LIMIT 1,
#          served by the (currency, valid_on) primary key) for both the expense currency and
#          the user's base currency (rates are stored as "to USD").
#  grid  - every category x {previous month, current month} so empty months give 0, not NULL.
#  agg   - GROUP BY category, month.
#  w     - LAG(total) over months = previous month's total.
MONTHLY_REPORT_SQL = text("""
WITH bounds AS (
    SELECT make_date(:year, :month, 1) AS cur,
           (make_date(:year, :month, 1) - interval '1 month')::date AS prev
),
conv AS (
    SELECT e.category_id,
           date_trunc('month', e.spent_on)::date AS mon,
           e.amount * er.rate_to_usd / br.rate_to_usd AS amount_base
    FROM expenses e
    JOIN users u ON u.id = e.user_id
    CROSS JOIN bounds b
    JOIN LATERAL (SELECT r.rate_to_usd FROM exchange_rates r
                  WHERE r.currency = e.currency AND r.valid_on <= e.spent_on
                  ORDER BY r.valid_on DESC LIMIT 1) er ON true
    JOIN LATERAL (SELECT r.rate_to_usd FROM exchange_rates r
                  WHERE r.currency = u.base_currency AND r.valid_on <= e.spent_on
                  ORDER BY r.valid_on DESC LIMIT 1) br ON true
    WHERE e.user_id = :user_id
      AND (CAST(:category_id AS bigint) IS NULL OR e.category_id = CAST(:category_id AS bigint))
      AND e.spent_on >= b.prev
      AND e.spent_on < (b.cur + interval '1 month')
),
grid AS (
    SELECT c.id AS category_id, c.name, c.monthly_budget, m.mon
    FROM categories c
    CROSS JOIN bounds b
    CROSS JOIN LATERAL (VALUES (b.prev), (b.cur)) AS m(mon)
    WHERE c.user_id = :user_id
      AND (CAST(:category_id AS bigint) IS NULL OR c.id = CAST(:category_id AS bigint))
),
agg AS (
    SELECT g.category_id, g.name, g.monthly_budget, g.mon,
           COALESCE(SUM(conv.amount_base), 0) AS total
    FROM grid g
    LEFT JOIN conv ON conv.category_id = g.category_id AND conv.mon = g.mon
    GROUP BY g.category_id, g.name, g.monthly_budget, g.mon
),
w AS (
    SELECT agg.*, LAG(total) OVER (PARTITION BY category_id ORDER BY mon) AS prev_total
    FROM agg
)
SELECT w.category_id,
       w.name AS category,
       w.monthly_budget AS budget,
       ROUND(w.total, 2) AS total,
       CASE WHEN w.monthly_budget > 0 THEN ROUND(w.total * 100 / w.monthly_budget, 2) END AS percent_used,
       ROUND(COALESCE(w.prev_total, 0), 2) AS previous_total,
       ROUND(w.total - COALESCE(w.prev_total, 0), 2) AS change_amount,
       CASE WHEN w.prev_total > 0 THEN ROUND((w.total - w.prev_total) * 100 / w.prev_total, 2) END
           AS change_percent
FROM w CROSS JOIN bounds b
WHERE w.mon = b.cur
ORDER BY w.name
""")


async def monthly_report(session: AsyncSession, user_id: int, year: int, month: int,
                         category_id: int | None = None) -> list[dict]:
    result = await session.execute(MONTHLY_REPORT_SQL, {
        "user_id": user_id, "year": year, "month": month, "category_id": category_id})
    return [dict(r) for r in result.mappings().all()]
