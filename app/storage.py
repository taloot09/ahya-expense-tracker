"""All S3 code lives here. boto3 is sync, so every call runs in a worker thread
(anyio.to_thread) and never blocks the event loop. Credentials come from the
default AWS chain (IAM role in production) - never from code."""
from functools import lru_cache

import anyio
import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.config import settings

CONTENT_TYPES = {"image/jpeg": "jpg", "image/png": "png", "application/pdf": "pdf"}
_MAGIC = [(b"\xff\xd8\xff", "jpg"), (b"\x89PNG\r\n\x1a\n", "png"), (b"%PDF-", "pdf")]


@lru_cache
def _client():
    return boto3.client("s3", region_name=settings.aws_region, endpoint_url=settings.s3_endpoint_url,
                        config=Config(signature_version="s3v4"))


def reset_client() -> None:
    _client.cache_clear()


def detect_type(head: bytes) -> str | None:
    """Real file type from magic bytes, never from the filename."""
    for magic, ext in _MAGIC:
        if head.startswith(magic):
            return ext
    return None


async def presigned_upload(key: str, content_type: str, expires: int = 300) -> dict:
    return await anyio.to_thread.run_sync(lambda: _client().generate_presigned_post(
        Bucket=settings.s3_bucket, Key=key,
        Fields={"Content-Type": content_type},
        Conditions=[{"Content-Type": content_type},
                    ["content-length-range", 1, settings.receipt_max_bytes]],
        ExpiresIn=expires))


async def object_size(key: str) -> int | None:
    def _head():
        try:
            return _client().head_object(Bucket=settings.s3_bucket, Key=key)["ContentLength"]
        except ClientError:
            return None
    return await anyio.to_thread.run_sync(_head)


async def read_first_bytes(key: str, n: int = 16) -> bytes:
    return await anyio.to_thread.run_sync(lambda: _client().get_object(
        Bucket=settings.s3_bucket, Key=key, Range=f"bytes=0-{n - 1}")["Body"].read())


async def delete_object(key: str) -> None:
    await anyio.to_thread.run_sync(lambda: _client().delete_object(Bucket=settings.s3_bucket, Key=key))


async def presigned_download(key: str, expires: int = 900) -> str:
    return await anyio.to_thread.run_sync(lambda: _client().generate_presigned_url(
        "get_object", Params={"Bucket": settings.s3_bucket, "Key": key}, ExpiresIn=expires))
