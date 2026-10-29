from tests.conftest import register_and_login


async def test_register_returns_201_and_hides_password(client):
    r = await client.post("/auth/register", json={"email": "a@example.com", "password": "password1",
                                                  "base_currency": "pkr"})
    assert r.status_code == 201
    body = r.json()
    assert body["base_currency"] == "PKR" and "password_hash" not in body


async def test_register_duplicate_email_409(client):
    await register_and_login(client)
    r = await client.post("/auth/register", json={"email": "ALICE@example.com", "password": "password1",
                                                  "base_currency": "PKR"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "email_taken"


async def test_register_rejects_bad_input_with_error_format(client):
    r = await client.post("/auth/register", json={"email": "nope", "password": "x", "base_currency": "XYZ"})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "validation_error" and len(err["details"]) == 3


async def test_login_wrong_password_401(client):
    await register_and_login(client)
    r = await client.post("/auth/login", json={"email": "alice@example.com", "password": "wrong-pass"})
    assert r.status_code == 401


async def test_sixth_failed_login_returns_429_with_retry_after(client):
    await register_and_login(client)
    for _ in range(5):
        r = await client.post("/auth/login", json={"email": "alice@example.com", "password": "bad"})
        assert r.status_code == 401
    r = await client.post("/auth/login", json={"email": "alice@example.com", "password": "s3cret-pass"})
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) > 0
    assert r.json()["error"]["code"] == "rate_limited"


async def test_refresh_rotates_tokens(client):
    tokens = await register_and_login(client)
    r = await client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 200
    assert r.json()["refresh_token"] != tokens["refresh_token"]


async def test_refresh_reuse_revokes_all_tokens(client):
    first = await register_and_login(client)
    second = (await client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]})).json()
    # Old token used again -> theft detected.
    r = await client.post("/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert r.status_code == 401 and r.json()["error"]["code"] == "token_reused"
    # The legit new refresh token is dead too...
    r = await client.post("/auth/refresh", json={"refresh_token": second["refresh_token"]})
    assert r.status_code == 401
    # ...and so is the access token.
    r = await client.get("/categories", headers={"Authorization": f"Bearer {second['access_token']}"})
    assert r.status_code == 401


async def test_logout_revokes_refresh_token(client):
    tokens = await register_and_login(client)
    r = await client.post("/auth/logout", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 204
    r = await client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert r.status_code == 401


async def test_missing_or_bad_token_401(client):
    assert (await client.get("/expenses")).status_code == 401
    r = await client.get("/expenses", headers={"Authorization": "Bearer garbage"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_token"


async def test_access_token_cannot_be_used_as_refresh(client):
    tokens = await register_and_login(client)
    r = await client.post("/auth/refresh", json={"refresh_token": tokens["access_token"]})
    assert r.status_code == 401
    r = await client.post("/auth/logout", json={"refresh_token": "junk"})
    assert r.status_code == 401


async def test_rate_limiter_fails_open_when_redis_down(client, monkeypatch):
    from redis.exceptions import ConnectionError as RedisConnError

    from app import redis_client
    await register_and_login(client)

    class Down:
        async def get(self, *a): raise RedisConnError("down")
        async def eval(self, *a): raise RedisConnError("down")
    monkeypatch.setattr(redis_client, "_client", Down())
    r = await client.post("/auth/login", json={"email": "alice@example.com", "password": "bad"})
    assert r.status_code == 401
    r = await client.post("/auth/login", json={"email": "alice@example.com", "password": "s3cret-pass"})
    assert r.status_code == 200
