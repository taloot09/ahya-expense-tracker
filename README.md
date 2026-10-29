# Ahya – Expense Tracker API

FastAPI · async SQLAlchemy 2.0 (asyncpg) · Pydantic v2 · Alembic · PostgreSQL · Redis · S3 (boto3, mocked with moto in tests).

**Tests: 47 passing · Coverage: 97%** (`pytest`, pytest-cov, real PostgreSQL + Redis).

## Run it

```bash
docker compose up -d                       # postgres (+ expense_test db), redis, minio
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env                       # edit JWT_SECRET
alembic upgrade head                       # schema + 90 days of PKR/AED/USD/EUR rates
uvicorn app.main:app --reload              # API  -> http://localhost:8000/docs
python -m app.worker                       # outbox worker (budget alerts, S3 cleanup)
pytest                                     # all tests + coverage
```

Postman collection: `postman/ahya-expense-tracker.postman_collection.json` (Login stores tokens automatically).
Sample CSV: `sample_import.csv` (columns `date,amount,currency,category,description`).

## Layout

```
app/
  main.py            app + routers
  config.py          env-based settings (no secrets in code)
  db.py / models.py  async engine, ORM models
  errors.py          single error format {"error":{"code","message","details"}}
  security.py        argon2 (in thread), JWT access/refresh
  ratelimit.py       Redis failed-login limiter (atomic Lua INCR+EXPIRE)
  storage.py         ALL S3 code (boto3 in worker threads, magic-byte detection)
  worker.py          outbox worker (FOR UPDATE SKIP LOCKED)
  routers/           auth, categories, expenses, receipts, imports, reports/notifications
  services/          expense creation + idempotency, report SQL, CSV importer, outbox helpers
alembic/versions/0001_initial.py   schema, indexes, rate seed
tests/               47 tests
```

## Design notes per requirement

**Money** – `Decimal` in Python, `NUMERIC(14,2)` amounts / `NUMERIC(18,8)` rates in Postgres; JSON output as strings. Pydantic rejects `0`, negatives and >2 decimal places; DB has `CHECK (amount > 0)`.

**Auth** – argon2 hashing runs via `anyio.to_thread` (CPU-bound, must not block the loop). Access 15 min, refresh 7 days. Each refresh token has a `jti` row. Refresh does an atomic `UPDATE ... SET revoked_at=now() WHERE jti=? AND revoked_at IS NULL RETURNING` — only one caller can consume a token. If the token exists but is already used/revoked → reuse = theft → all the user's refresh tokens are revoked **and** `users.token_version` is bumped, which kills every outstanding access token too (the access JWT carries `tv`, checked on each request).

**Rate limit** – Redis key `login_fail:{email}`, Lua `INCR` + `EXPIRE` on first failure (atomic, shared by all API servers). 5 failures in 15 min → 429 + `Retry-After` = remaining TTL.

**Data access** – every query filters by `user_id`; foreign rows return 404.

**Listing** – keyset pagination on `(spent_on DESC, id DESC)` with `WHERE (spent_on, id) < (:d, :id)`; cursor is base64 JSON. Index `(user_id, spent_on DESC, id DESC)`. `q` uses escaped `ILIKE`. `min/max_amount` filter the original amount (not converted).

**Monthly report** – one SQL statement (`app/services/reports.py`): `LATERAL` lookup of the latest rate `<= spent_on` for both the expense currency and the base currency (missing day → previous rate), a category × {prev, current month} grid so empty months show 0, `GROUP BY`, then `LAG(total) OVER (PARTITION BY category ORDER BY month)`.

**Indexes**
| Index | Why |
|---|---|
| `expenses (user_id, spent_on DESC, id DESC)` | list + keyset pagination: index-ordered scan, no sort |
| `expenses (user_id, category_id, spent_on)` | report / budget check for one category and month |
| `UNIQUE expenses (user_id, import_ref)` | CSV de-duplication |
| `exchange_rates PK (currency, valid_on)` | "latest rate on or before date" = one index probe backwards |
| `UNIQUE categories (user_id, name)` | duplicate → 409; also serves "my categories" |
| `refresh_tokens (user_id)` | revoke-all on theft |
| `PK idempotency_keys (user_id, key)` + `(expires_at)` | race-safe idempotency; purge job |
| `outbox_events (id) WHERE processed_at IS NULL` | worker poll touches only pending rows |
| `UNIQUE notifications (user_id, category_id, year, month, level)` | alert exactly once |

### 2.1 Idempotency-Key
The key row is inserted **first, inside the same transaction** as the expense (`INSERT ... ON CONFLICT DO NOTHING`, PK `(user_id, key)`). A concurrent request with the same key blocks on the unique index until the first transaction commits, then gets "conflict" and returns the stored response (`Idempotent-Replayed: true`). If the first one fails (e.g. 404 category) it rolls back and the key is free again. Different body (SHA-256 of canonical JSON) → 422. Keys expire after 24 h (`expires_at`; expired rows are ignored/replaced and purged by the worker).
*Why DB only, no Redis lock:* the guarantee comes from the same transaction that writes the expense, so it can't drift (a Redis lock can expire mid-request or be lost on failover) and idempotency survives a Redis outage. Test: 5 parallel POSTs via `asyncio.gather` → one expense.

### 2.2 ETag / If-Match
`ETag: "<version>"`. PATCH without `If-Match` → 428. Update is one statement: `UPDATE expenses SET ..., version=version+1 WHERE id=? AND user_id=? AND version=? RETURNING *`. Two parallel writers serialize on the row lock; the loser re-checks `version=?`, matches 0 rows → 412.

### 2.3 Budget alerts (transactional outbox)
Create/update/import write the expense **and** a `budget.check` outbox row in one transaction. `python -m app.worker` claims batches with `SELECT ... FOR UPDATE SKIP LOCKED` (parallel workers never take the same event), recomputes the category's month total with the report SQL, and inserts notifications with `ON CONFLICT DO NOTHING` on `UNIQUE(user, category, year, month, level)`. Redelivery after a crash or two workers → still one notification.
**Edits/deletes:** a PATCH enqueues a check for the expense's *new* category/month (the only place the total can go up). Deletes and decreases never un-send an alert: an alert is a fact about a moment ("you crossed 80%"); if the total drops and crosses again in the same month, no second alert is sent (once per level per month, as required).

### 2.4 CSV import
Upload is streamed to a temp file in 1 MB chunks while computing SHA-256 → 202 + `job_id`. Processing runs in the background, reading 1,000 rows at a time in a worker thread (never the whole file in memory), validating each row (date, amount >0 & ≤2 dp, currency, category name), one `INSERT ... ON CONFLICT DO NOTHING` per batch and a commit per batch (the loop yields so other requests stay fast). Duplicates: same file for the same user → same job (`UNIQUE(user_id, file_hash)`); additionally each row gets `import_ref = "<file hash>:<row number>"` with a unique index, so a resumed/retried job can't double insert. Max 100,000 rows. Errors list stores up to 1,000 `{row, reason}` entries.

### 2.5 Receipts
`storage.py` holds all S3 code. Pre-signed **POST** with `content-length-range 1..5 MB` and a fixed `Content-Type` (jpg/png/pdf only), key `receipts/{user_id}/{expense_id}/{uuid}.{ext}`, 5 min expiry. Confirm: key must be under this user's/expense's prefix, `HEAD` for existence/size, ranged `GET` of the first bytes to detect the real type from magic bytes; mismatch (e.g. `.exe` renamed `.png`) → object deleted + 422. Download link expires in 15 min. Deleting an expense writes a `receipt.delete` outbox event in the same transaction; the worker deletes the object and retries on failure (attempts/last_error kept) — the file can't be forgotten. Replacing a receipt also queues the old key for deletion.

## AI usage
I used an AI assistant (Claude) to speed up writing boilerplate and a first draft of the code, tests and this README. I reviewed, ran and fixed everything, and the design decisions (DB-based idempotency, single-statement optimistic locking, outbox with SKIP LOCKED + unique constraint, keyset pagination, one-query report) are explained above and I can walk through every line.

## Not finished / would improve
- **Import worker placement:** import runs as a FastAPI background task on the server that received the file. In production I'd upload the CSV to S3 and push the job to the worker (arq/Redis queue), so jobs survive restarts and any server can process them; a job stuck in `processing` would be resumed (row-level `import_ref` already makes resuming safe). I'd also use `COPY` into a staging table for faster 100k-row loads.
- **Orphaned uploads:** a file uploaded but never confirmed stays in S3 → S3 lifecycle rule to expire unconfirmed objects (or tag-on-confirm + lifecycle on untagged).
- **Search at scale:** `pg_trgm` GIN index on `description` for `q`.
- **Outbox retention:** delete processed outbox rows older than N days (partition by month).
- Rates for dates before the first seeded rate aren't validated at create time (such an expense is skipped in reports); I'd reject or fetch the rate.
- No Dockerfile for the app itself (compose runs dependencies only).

---

## Deployment notes (AWS)

**1. API and worker.** ECS on Fargate, two services from one image: `api` (uvicorn, min 2 tasks across 2 AZs behind an ALB, target-tracking autoscaling on CPU ~60% / request count, health check `/health`) and `worker` (min 1, scales on outbox backlog via a CloudWatch custom metric). ECS restarts failed tasks automatically. Zero-downtime release = rolling deployment (`minimumHealthyPercent=100`, `maximumPercent=200`): new tasks must pass ALB health checks before old ones are drained (deregistration delay lets in-flight requests finish). Circuit breaker with rollback enabled.

**2. Database.** RDS PostgreSQL Multi-AZ in private subnets, no public access. Security group: inbound **5432 only from the API and worker task security groups** (SG-to-SG reference, no CIDRs). The DB password lives in Secrets Manager (RDS-managed, rotated); the ECS task definition injects it via `secrets` → env var, read by the task execution role. Nothing hardcoded.

**3. Redis.** ElastiCache Redis (replication group, Multi-AZ, TLS, in-transit auth), SG allows 6379 from app SGs only. If Redis goes down: **idempotency is unaffected** (it's in Postgres). **Login rate limiting fails open** (`app/ratelimit.py` catches `RedisError`, logs, and lets the login proceed): locking every user out during a Redis outage is worse than briefly losing the limiter, since a correct password is still required and argon2 makes guessing slow. A CloudWatch alarm on the error log/ElastiCache health pages on-call. Refresh tokens are in Postgres, so auth keeps working.

**4. S3.** Private bucket, Block Public Access on, SSE-S3 (or KMS), lifecycle rule for unconfirmed uploads. The task role (no access keys) gets only:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReceiptObjects",
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::expense-receipts/receipts/*"
    }
  ]
}
```
(`HeadObject` is authorized by `s3:GetObject`; pre-signed URLs inherit the role's permissions, so they also can only touch `receipts/*`.)

**5. Migrations.** Expand/contract. Migrations run as a one-off ECS task (`alembic upgrade head`) in the pipeline **before** the new app version is deployed. Every migration must be backward compatible with the currently running version: add nullable columns / new tables / indexes `CONCURRENTLY`; never rename or drop in the same release. Old and new code both work during the rollout. Destructive changes (drop old column) ship in a later release once no code uses them.

## Short answers

**1. Pre-signed upload URLs.** The file goes straight from the phone to S3, so the API doesn't hold 5 MB bodies in memory, doesn't pay bandwidth twice, and API workers stay free for short requests; S3 handles scale and retries. Risks: the URL is a bearer credential (anyone who gets it can upload) → short expiry (5 min), key bound to `user/expense/uuid`, one key per request. Clients can upload something other than claimed → `content-length-range` and fixed `Content-Type` in the POST policy, plus server-side magic-byte check on confirm and deletion of bad files. Uploads that are never confirmed → lifecycle cleanup. Never let the client choose the key, and keep the bucket private (download only via 15-min GET URLs).

**2. GET /expenses slow at 20M rows.** First reproduce with the real filters and run `EXPLAIN (ANALYZE, BUFFERS)`: look for Seq Scan on expenses, a big `Sort` node (ordering not served by an index), high `shared read` buffers (disk), and rows-removed-by-filter. The fix is the composite index `(user_id, spent_on DESC, id DESC)`, which matches the WHERE + ORDER BY exactly, so Postgres walks the index and stops after `LIMIT` rows. Offset pagination gets slower with every page because the DB must produce and throw away all previous `OFFSET` rows; keyset (`(spent_on,id) < cursor`) is a constant-cost index seek. For `q`, add a `pg_trgm` GIN index. Check for N+1: if the response embedded category names fetched per row, that's 1 + N queries — use a join/`selectinload`. Then confirm with EXPLAIN again and monitor `pg_stat_statements`.

**3. Three servers behind a load balancer.** Tokens: stateless JWT with a shared secret, refresh state in Postgres → any server works. Rate limits: counters in Redis with an atomic Lua script, not in memory → global per email. Idempotency: Postgres unique key inside the transaction → safe across servers. Worker: outbox claimed with `FOR UPDATE SKIP LOCKED` and notifications deduped by a unique constraint → any number of workers. Optimistic locking is a single SQL statement → safe. File uploads: go directly to S3, nothing on local disk — except the CSV import temp file, which is processed by the same server (known gap; fix = S3 + queue). Migrations: run once as a separate task before rollout, never on app start (3 servers would race).

**4. Worker down for 2 hours, 500k events.** Nothing is lost: events sit in the outbox (committed with the expenses). When it returns it drains in batches of 100 using the partial index on pending rows, so each poll is cheap. To avoid hammering the DB: fixed batch size, a cap on concurrent workers, a small sleep between batches when backlog is huge (or a max events/sec), and processing in a separate DB connection pool. Better: coalesce — many `budget.check` events for the same (user, category, month) only need one recomputation, so dedupe by that key per batch (`DISTINCT ON`). Alerts arrive late but exactly once. Alarm on `count(pending)` and oldest pending age.

**5. Simple over "perfect".** The outbox worker polls Postgres every second instead of using LISTEN/NOTIFY, Kafka or a Redis stream. Polling with `SKIP LOCKED` on a partial index is a few microseconds per idle poll, needs no extra infrastructure, is trivially correct with multiple workers, and can't lose a message because the outbox *is* the queue. Alerts being up to a second late doesn't matter for budgets. Similarly, the CSV importer runs as an in-process background task instead of a separate queue — simpler to build and test in 48 h, with the trade-off documented above.
