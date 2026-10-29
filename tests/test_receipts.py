import boto3
import pytest
from moto import mock_aws

from app import storage, worker
from app.config import settings
from tests.conftest import make_category, make_expense

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
EXE = b"MZ\x90\x00" + b"\x00" * 100


@pytest.fixture
def s3(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setattr(settings, "s3_endpoint_url", None)
    with mock_aws():
        storage.reset_client()
        client = boto3.client("s3", region_name=settings.aws_region)
        client.create_bucket(Bucket=settings.s3_bucket)
        yield client
    storage.reset_client()


async def _expense(client, auth):
    cid = await make_category(client, auth)
    return await make_expense(client, auth, cid)


async def test_upload_url_and_confirm_real_png(client, auth, s3):
    e = await _expense(client, auth)
    r = await client.post(f"/expenses/{e['id']}/receipt/upload-url", headers=auth, json={"content_type": "image/png"})
    assert r.status_code == 200
    key = r.json()["key"]
    assert key.startswith(f"receipts/1/{e['id']}/") and key.endswith(".png")
    assert "content-length-range" in str(r.json()["fields"]) or r.json()["fields"]
    s3.put_object(Bucket=settings.s3_bucket, Key=key, Body=PNG)
    r = await client.post(f"/expenses/{e['id']}/receipt/confirm", headers=auth, json={"key": key})
    assert r.status_code == 200
    r = await client.get(f"/expenses/{e['id']}/receipt", headers=auth)
    assert r.status_code == 200 and "Expires=900" in r.json()["url"] or "X-Amz-Expires=900" in r.json()["url"]


async def test_exe_renamed_to_png_is_rejected_and_deleted(client, auth, s3):
    e = await _expense(client, auth)
    key = (await client.post(f"/expenses/{e['id']}/receipt/upload-url", headers=auth,
                             json={"content_type": "image/png"})).json()["key"]
    s3.put_object(Bucket=settings.s3_bucket, Key=key, Body=EXE)
    r = await client.post(f"/expenses/{e['id']}/receipt/confirm", headers=auth, json={"key": key})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_file"
    assert s3.list_objects_v2(Bucket=settings.s3_bucket).get("KeyCount", 0) == 0


async def test_unsupported_type_and_foreign_key_rejected(client, auth, s3):
    e = await _expense(client, auth)
    r = await client.post(f"/expenses/{e['id']}/receipt/upload-url", headers=auth, json={"content_type": "text/html"})
    assert r.status_code == 422
    r = await client.post(f"/expenses/{e['id']}/receipt/confirm", headers=auth,
                          json={"key": "receipts/999/1/x.png"})
    assert r.status_code == 422
    r = await client.post(f"/expenses/{e['id']}/receipt/confirm", headers=auth,
                          json={"key": f"receipts/1/{e['id']}/missing.png"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "receipt_not_found"
    assert (await client.get(f"/expenses/{e['id']}/receipt", headers=auth)).status_code == 404


async def test_deleting_expense_deletes_file_via_outbox(client, auth, s3):
    e = await _expense(client, auth)
    key = (await client.post(f"/expenses/{e['id']}/receipt/upload-url", headers=auth,
                             json={"content_type": "image/png"})).json()["key"]
    s3.put_object(Bucket=settings.s3_bucket, Key=key, Body=PNG)
    await client.post(f"/expenses/{e['id']}/receipt/confirm", headers=auth, json={"key": key})
    assert (await client.delete(f"/expenses/{e['id']}", headers=auth)).status_code == 204
    assert s3.list_objects_v2(Bucket=settings.s3_bucket)["KeyCount"] == 1  # not inside the request
    await worker.process_batch()
    assert s3.list_objects_v2(Bucket=settings.s3_bucket).get("KeyCount", 0) == 0


def test_detect_type_magic_bytes():
    assert storage.detect_type(b"\xff\xd8\xff\xe0") == "jpg"
    assert storage.detect_type(b"%PDF-1.7") == "pdf"
    assert storage.detect_type(PNG) == "png"
    assert storage.detect_type(EXE) is None
