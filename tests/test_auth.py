import io
import json
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from app import auth, governance, main, tokens

PRIYA = "priya@northbeam.com"
MARCUS = "marcus@northbeam.com"
SUPABASE = "https://abcd.supabase.co"
SECRET = "test-secret-at-least-32-bytes-long!!"


def supabase_token(email=PRIYA, key=SECRET, alg="HS256", **over):
    claims = {"sub": "u1", "email": email, "aud": "authenticated", "iss": f"{SUPABASE}/auth/v1",
              "exp": int(time.time()) + 600, **over}
    return jwt.encode(claims, key, algorithm=alg)


@pytest.fixture
def supabase(monkeypatch):
    monkeypatch.setenv("BRAIN_AUTH_MODE", "supabase")
    monkeypatch.setenv("SUPABASE_URL", SUPABASE)
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)


def test_api_tokens_work_in_every_mode(monkeypatch):
    token = tokens.create(PRIYA)
    for mode in ("tokens", "supabase", "local"):
        monkeypatch.setenv("BRAIN_AUTH_MODE", mode)
        assert auth.user_for_token(token) == PRIYA


def test_sign_in_tokens_are_ignored_unless_enabled(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", SUPABASE)
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SECRET)
    assert auth.user_for_token(supabase_token()) is None


def test_supabase_token_identifies_a_brain_user(supabase):
    assert auth.user_for_token(supabase_token()) == PRIYA
    assert auth.user_for_token(supabase_token(email="Priya@Northbeam.com")) == PRIYA
    assert auth.from_header(f"Bearer {supabase_token()}") == PRIYA


@pytest.mark.parametrize("bad", [
    {"key": "another-secret-that-is-also-32-bytes!"},
    {"exp": int(time.time()) - 10},
    {"aud": "anon"},
    {"iss": "https://evil.supabase.co/auth/v1"},
    {"email": "stranger@example.com"},
])
def test_supabase_token_refused(supabase, bad):
    key = bad.pop("key", SECRET)
    assert auth.user_for_token(supabase_token(key=key, **bad)) is None


def test_supabase_signing_keys(monkeypatch):
    monkeypatch.setenv("BRAIN_AUTH_MODE", "supabase")
    monkeypatch.setenv("SUPABASE_URL", SUPABASE)
    monkeypatch.delenv("SUPABASE_JWT_SECRET", raising=False)
    private = ec.generate_private_key(ec.SECP256R1())
    fetched = []

    class Keys:
        def get_signing_key_from_jwt(self, token):
            return SimpleNamespace(key=private.public_key())

    monkeypatch.setattr(auth, "_jwks", lambda url: fetched.append(url) or Keys())
    assert auth.user_for_token(supabase_token(key=private, alg="ES256")) == PRIYA
    assert fetched == [f"{SUPABASE}/auth/v1/.well-known/jwks.json"]
    other = ec.generate_private_key(ec.SECP256R1())
    assert auth.user_for_token(supabase_token(key=other, alg="ES256")) is None


def test_local_auth_server_vouches(monkeypatch):
    monkeypatch.setenv("BRAIN_AUTH_MODE", "local")
    seen = []

    def urlopen(request, timeout):
        seen.append(json.loads(request.data))
        return io.BytesIO(json.dumps({"user": {"id": "u1", "email": MARCUS}}).encode())

    monkeypatch.setattr(auth.urllib.request, "urlopen", urlopen)
    assert auth.user_for_token("dev-token") == MARCUS
    assert seen == [{"accessToken": "dev-token"}]

    def down(request, timeout):
        raise OSError("refused")

    monkeypatch.setattr(auth.urllib.request, "urlopen", down)
    assert auth.user_for_token("dev-token") is None


def test_session_cookie():
    cookie = auth.session_cookie(PRIYA, now=1000)
    assert auth.user_for_cookie(cookie, now=1001) == PRIYA
    assert auth.user_for_cookie(cookie, now=1000 + auth.SESSION_SECONDS + 1) is None
    assert auth.user_for_cookie(cookie.replace(PRIYA, MARCUS), now=1001) is None
    assert auth.user_for_cookie("garbage", now=1001) is None
    assert auth.user_for_cookie(None) is None


def test_mcp_accepts_a_sign_in_token(supabase):
    client = TestClient(main.app)
    res = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                      headers={"authorization": f"Bearer {supabase_token()}"})
    assert res.status_code == 200


# ----------------------------------------------------------------- BRAIN_REQUIRE_AUTH

@pytest.fixture
def locked(monkeypatch):
    monkeypatch.setenv("BRAIN_REQUIRE_AUTH", "1")
    return TestClient(main.app)


def signed_in(client, email=PRIYA):
    res = client.post("/api/session", json={"token": tokens.create(email)})
    assert res.status_code == 200
    assert res.cookies.get(auth.COOKIE)
    return client


def test_open_by_default():
    client = TestClient(main.app)
    assert client.get("/api/me", params={"user": MARCUS}).status_code == 200
    assert len(client.get("/api/users").json()) > 1


def test_locked_api_needs_sign_in(locked):
    assert locked.get("/api/auth-config").json() == {"require_auth": True, "mode": "tokens"}
    assert locked.get("/api/me", params={"user": PRIYA}).status_code == 401
    assert locked.get("/files/anything", params={"user": PRIYA}).status_code == 401
    assert locked.post("/api/session", json={"token": "pb_wrong"}).status_code == 401


def test_signed_in_caller_acts_only_as_themselves(locked):
    signed_in(locked)
    assert locked.get("/api/me", params={"user": PRIYA}).status_code == 200
    assert [u["email"] for u in locked.get("/api/users").json()] == [PRIYA]
    assert locked.get("/api/me", params={"user": MARCUS}).status_code == 403
    posted = locked.post("/api/access-requests", json={"user": MARCUS, "reason": "x"})
    assert posted.status_code == 403
    assert (PRIYA, "blocked") in [(e["email"], e["action"]) for e in governance.audit_log()]
    # The body still reaches the handler when it names the caller.
    assert locked.post("/api/access-requests", json={"user": PRIYA, "reason": "Need Harbor for a comp"}).status_code == 200


def test_bearer_works_on_the_locked_api(locked):
    res = locked.get("/api/me", params={"user": MARCUS}, headers={"authorization": f"Bearer {tokens.create(MARCUS)}"})
    assert res.status_code == 200


def test_signing_out(locked):
    signed_in(locked)
    locked.delete("/api/session")
    locked.cookies.clear()
    assert locked.get("/api/me", params={"user": PRIYA}).status_code == 401


def test_app_api_is_unaffected_by_the_gate(locked):
    res = locked.get("/api/v1/me", headers={"authorization": f"Bearer {tokens.create(PRIYA)}"})
    assert res.status_code == 200
