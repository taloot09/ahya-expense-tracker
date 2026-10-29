from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import redis_client
from app.db import engine
from app.errors import install_error_handlers
from app.routers import auth, categories, expenses, imports, misc, receipts


@asynccontextmanager
async def lifespan(_: FastAPI):  # pragma: no cover
    yield
    if redis_client._client is not None:
        await redis_client._client.aclose()
    await engine.dispose()


app = FastAPI(title="Ahya Expense Tracker", version="1.0.0", lifespan=lifespan)
install_error_handlers(app)
for r in (auth.router, categories.router, imports.router, expenses.router, receipts.router, misc.router):
    app.include_router(r, prefix="/api/v1")


@app.get("/health")
async def health():
    return {"status": "ok"}
