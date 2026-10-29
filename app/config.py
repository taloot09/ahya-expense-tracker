from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All config comes from environment variables (or a local, git-ignored .env)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://expense:expense@localhost:5432/expense"
    redis_url: str = "redis://localhost:6379/0"

    # Dev-only placeholder. Production MUST set JWT_SECRET from Secrets Manager/SSM.
    jwt_secret: str = "dev-only-insecure-secret-change-me"
    jwt_algorithm: str = "HS256"
    access_token_minutes: int = 15
    refresh_token_days: int = 7

    login_max_failures: int = 5
    login_window_seconds: int = 900

    idempotency_ttl_hours: int = 24

    s3_bucket: str = "expense-receipts"
    s3_endpoint_url: str | None = None  # set for MinIO/LocalStack; None = real AWS
    aws_region: str = "us-east-1"
    receipt_max_bytes: int = 5 * 1024 * 1024

    import_max_rows: int = 100_000
    import_batch_size: int = 1000


settings = Settings()
