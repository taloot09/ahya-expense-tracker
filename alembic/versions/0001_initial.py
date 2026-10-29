"""initial schema + 90 days of exchange rates

Revision ID: 0001
Revises:
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("base_currency", sa.String(3), nullable=False),
        sa.Column("token_version", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "refresh_tokens",
        sa.Column("jti", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("expires_at", TS, nullable=False),
        sa.Column("revoked_at", TS),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
    )
    # Revoke-all-for-user on token theft.
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])
    op.create_table(
        "categories",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("monthly_budget", sa.Numeric(14, 2)),
        # Also the index for "my categories" (user_id is its leading column).
        sa.UniqueConstraint("user_id", "name", name="uq_categories_user_name"),
    )
    op.create_table(
        "expenses",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("category_id", sa.BigInteger, sa.ForeignKey("categories.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("spent_on", sa.Date, nullable=False),
        sa.Column("receipt_key", sa.String(512)),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("import_ref", sa.String(100)),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("amount > 0", name="ck_expenses_amount_positive"),
        sa.UniqueConstraint("user_id", "import_ref", name="uq_expenses_user_import_ref"),
    )
    # GET /expenses: WHERE user_id=? ORDER BY spent_on DESC, id DESC + keyset (spent_on,id) < (?,?)
    op.execute("CREATE INDEX ix_expenses_user_spent_id ON expenses (user_id, spent_on DESC, id DESC)")
    # Reports / budget checks: one user, one category, a date range.
    op.create_index("ix_expenses_user_category_spent", "expenses", ["user_id", "category_id", "spent_on"])
    op.create_table(
        "exchange_rates",
        sa.Column("currency", sa.String(3), primary_key=True),
        sa.Column("valid_on", sa.Date, primary_key=True),  # PK serves "latest rate <= date" lookups
        sa.Column("rate_to_usd", sa.Numeric(18, 8), nullable=False),
    )
    op.create_table(
        "idempotency_keys",
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("key", sa.String(255), primary_key=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status_code", sa.Integer),
        sa.Column("response_body", postgresql.JSONB),
        sa.Column("expires_at", TS, nullable=False),
    )
    op.create_index("ix_idempotency_keys_expires_at", "idempotency_keys", ["expires_at"])  # purge job
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("processed_at", TS),
    )
    # Worker poll: only unprocessed rows are indexed, so it stays tiny.
    op.create_index("ix_outbox_pending", "outbox_events", ["id"], postgresql_where=sa.text("processed_at IS NULL"))
    op.create_table(
        "notifications",
        sa.Column("id", sa.BigInteger, primary_key=True),
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("category_id", sa.BigInteger, sa.ForeignKey("categories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("year", sa.Integer, nullable=False),
        sa.Column("month", sa.Integer, nullable=False),
        sa.Column("level", sa.Integer, nullable=False),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "category_id", "year", "month", "level", name="uq_notification_once"),
    )
    op.create_table(
        "import_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", sa.BigInteger, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("file_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("rows_done", sa.Integer, nullable=False),
        sa.Column("rows_failed", sa.Integer, nullable=False),
        sa.Column("errors", postgresql.JSONB, nullable=False),
        sa.Column("created_at", TS, nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", TS),
        sa.UniqueConstraint("user_id", "file_hash", name="uq_import_user_file"),
    )

    # Seed: last 90 days for PKR, AED, USD, EUR (rate = value of 1 unit in USD).
    op.execute("""
        INSERT INTO exchange_rates (currency, valid_on, rate_to_usd)
        SELECT c.currency, d::date, c.rate
        FROM (VALUES ('USD', 1.0), ('PKR', 0.00357143), ('AED', 0.27229408), ('EUR', 1.08000000))
             AS c(currency, rate)
        CROSS JOIN generate_series(current_date - 89, current_date, interval '1 day') AS d
        ON CONFLICT DO NOTHING
    """)


def downgrade() -> None:
    for t in ("import_jobs", "notifications", "outbox_events", "idempotency_keys", "exchange_rates",
              "expenses", "categories", "refresh_tokens", "users"):
        op.drop_table(t)
