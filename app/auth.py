"""Who is calling: API tokens, sign-in sessions from the firm's identity provider, and the web UI's cookie.

Bearer credentials accepted everywhere a caller is identified:
- an API token (`pb_...`, app/tokens.py), always;
- with BRAIN_AUTH_MODE=supabase, a Supabase Auth access token, checked against the project's signing keys
  (SUPABASE_URL, JWKS) or its legacy shared secret (SUPABASE_JWT_SECRET), issuer and audience included;
- with BRAIN_AUTH_MODE=local, a token from Tessel's dev auth server (LOCAL_AUTH_URL), which vouches for it.
Either way the caller must also be a user in the brain: signing in proves who you are, staffing decides
what you see.

BRAIN_REQUIRE_AUTH=1 closes the demo user switcher: every /api and /files request must carry a credential
(bearer or the session cookie from POST /api/session), and a `user` parameter must name the caller.
"""

import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.request
from functools import lru_cache

from app import store, tokens

COOKIE = "pb_session"
SESSION_SECONDS = 12 * 3600
# Without BRAIN_SESSION_SECRET, sessions last until the server restarts.
_process_secret = secrets.token_bytes(32)


def mode() -> str:
    chosen = os.environ.get("BRAIN_AUTH_MODE", "tokens").strip().lower()
    return chosen if chosen in ("tokens", "supabase", "local") else "tokens"


def require_auth() -> bool:
    return os.environ.get("BRAIN_REQUIRE_AUTH", "").strip().lower() in ("1", "true", "yes")


def _known(email: str | None) -> str | None:
    if not email:
        return None
    user = store._rows("SELECT email FROM users WHERE lower(email) = lower(?)", (email,))
    return user[0]["email"] if user else None


@lru_cache(maxsize=4)
def _jwks(url: str):
    import jwt

    return jwt.PyJWKClient(url, cache_keys=True, lifespan=3600)


def _supabase_email(token: str) -> str | None:
    import jwt

    base = os.environ.get("SUPABASE_URL", "").rstrip("/")
    secret = os.environ.get("SUPABASE_JWT_SECRET", "")
    if not base:
        return None
    issuer = f"{base}/auth/v1"
    try:
        if secret:
            claims = jwt.decode(token, secret, algorithms=["HS256"], audience="authenticated", issuer=issuer)
        else:
            key = _jwks(f"{issuer}/.well-known/jwks.json").get_signing_key_from_jwt(token)
            claims = jwt.decode(token, key.key, algorithms=["ES256", "RS256"], audience="authenticated", issuer=issuer)
    except Exception:  # any invalid, expired or unverifiable token is refused
        return None
    email = claims.get("email")
    return email if isinstance(email, str) else None


def _local_email(token: str) -> str | None:
    url = os.environ.get("LOCAL_AUTH_URL", "http://localhost:8788").rstrip("/")
    request = urllib.request.Request(
        f"{url}/auth/session", data=json.dumps({"accessToken": token}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as res:
            data = json.loads(res.read())
    except Exception:  # unreachable verifier refuses; never falls open
        return None
    email = (data.get("user") or {}).get("email")
    return email if isinstance(email, str) else None


def user_for_token(token: str) -> str | None:
    token = token.strip()
    if not token:
        return None
    if token.startswith(tokens.PREFIX):
        return tokens.user_for(token)
    if mode() == "supabase":
        return _known(_supabase_email(token))
    if mode() == "local":
        return _known(_local_email(token))
    return None


def from_header(authorization: str | None) -> str | None:
    auth = authorization or ""
    return user_for_token(auth[7:]) if auth[:7].lower() == "bearer " else None


# ----------------------------------------------------------------- web UI session cookie

def _secret() -> bytes:
    configured = os.environ.get("BRAIN_SESSION_SECRET", "")
    return configured.encode() if configured else _process_secret


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()


def session_cookie(email: str, now: float | None = None) -> str:
    payload = f"{email}|{int((now or time.time()) + SESSION_SECONDS)}"
    return f"{payload}|{_sign(payload)}"


def user_for_cookie(value: str | None, now: float | None = None) -> str | None:
    if not value or value.count("|") != 2:
        return None
    email, expires, signature = value.split("|")
    if not hmac.compare_digest(signature, _sign(f"{email}|{expires}")):
        return None
    if not expires.isdigit() or int(expires) < (now or time.time()):
        return None
    return _known(email)
