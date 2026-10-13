"""Bearer tokens for API clients (the MCP endpoint), one or more per user.

The token identifies the caller, so a client never names the user it acts for. Only a SHA-256 hash is
stored: a copy of brain.db does not hand out working tokens.
"""

import hashlib
import secrets
from datetime import datetime, timezone

from app import store

PREFIX = "pb_"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create(email: str, label: str = "") -> str:
    """Issue a token for a known user. The plain token is returned once and never stored."""
    if not store.get_user(email):
        raise ValueError(f"Unknown user {email}")
    token = PREFIX + secrets.token_urlsafe(32)
    store.execute(
        "INSERT INTO api_tokens(token_hash, email, label, created_at) VALUES (?,?,?,?)",
        (_hash(token), email, label, _now()),
    )
    return token


def user_for(token: str) -> str | None:
    """The email a token belongs to, or None when it is unknown, revoked or its user was removed."""
    if not token.startswith(PREFIX):
        return None
    rows = store._rows("SELECT email FROM api_tokens WHERE token_hash = ?", (_hash(token),))
    if not rows or not store.get_user(rows[0]["email"]):
        return None
    store.execute("UPDATE api_tokens SET last_used_at = ? WHERE token_hash = ?", (_now(), _hash(token)))
    return rows[0]["email"]


def list_for(email: str) -> list[dict]:
    return store._rows(
        "SELECT substr(token_hash, 1, 8) AS id, label, created_at, last_used_at FROM api_tokens WHERE email = ? ORDER BY created_at",
        (email,),
    )


def revoke(email: str, token_id: str) -> bool:
    """Revoke one of a user's tokens by the id `list_for` shows (the first 8 hex digits of its hash)."""
    matches = store._rows(
        "SELECT token_hash FROM api_tokens WHERE email = ? AND substr(token_hash, 1, 8) = ?", (email, token_id)
    )
    if len(matches) != 1:
        return False
    store.execute("DELETE FROM api_tokens WHERE token_hash = ?", (matches[0]["token_hash"],))
    return True
