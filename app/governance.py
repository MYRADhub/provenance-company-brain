"""Ethical-wall administration: deal staffing, access requests and the audit trail.

Brain documents carry a deal principal (group:<firm>:<deal>, config.deal_group), while staffing lives here in SQLite.
A staffing change therefore takes effect on the user's next query without re-indexing anything.
"""

import re
from datetime import datetime, timezone

from app import store

ADMIN_ROLES = ("managing_partner", "compliance")


class GovernanceError(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_admin(email: str) -> bool:
    user = store.get_user(email)
    return bool(user) and user["role"] in ADMIN_ROLES


def _require_admin(email: str) -> None:
    if not is_admin(email):
        raise GovernanceError("Only Compliance and the Managing Partner can manage access.")


def log(email: str, action: str, detail: str = "", outcome: str = "ok") -> None:
    store.execute(
        "INSERT INTO access_log(ts, email, action, detail, outcome) VALUES (?,?,?,?,?)",
        (_now(), email, action, detail[:500], outcome),
    )


def audit_log(limit: int = 100) -> list[dict]:
    return store._rows("SELECT * FROM access_log ORDER BY id DESC LIMIT ?", (limit,))


def overview(admin_email: str) -> dict:
    _require_admin(admin_email)
    deals = store._rows("SELECT id, codename, company, sector, status FROM deals ORDER BY codename")
    members = store._rows("SELECT deal_id, email FROM deal_members")
    for d in deals:
        d["team"] = sorted(m["email"] for m in members if m["deal_id"] == d["id"])
    return {
        "deals": deals,
        "users": store.list_users(),
        "requests": list_requests(admin_email),
        "log": audit_log(),
    }


def set_member(admin_email: str, deal_id: str, member_email: str, staffed: bool) -> None:
    _require_admin(admin_email)
    if not store._rows("SELECT 1 FROM deals WHERE id = ?", (deal_id,)):
        raise GovernanceError("Unknown deal")
    if not store.get_user(member_email):
        raise GovernanceError("Unknown person")
    if staffed:
        store.execute("INSERT OR IGNORE INTO deal_members(deal_id, email) VALUES (?,?)", (deal_id, member_email))
    else:
        store.execute("DELETE FROM deal_members WHERE deal_id = ? AND email = ?", (deal_id, member_email))
    log(admin_email, "staffing", f"{'added' if staffed else 'removed'} {member_email} {'to' if staffed else 'from'} {deal_id}")


def request_access(email: str, reason: str) -> dict:
    if not store.get_user(email):
        raise GovernanceError("Unknown person")
    reason = reason.strip()
    if not reason:
        raise GovernanceError("Say what you need access to and why.")
    store.execute(
        "INSERT INTO access_requests(created_at, email, reason, status) VALUES (?,?,?, 'pending')",
        (_now(), email, reason[:1000]),
    )
    log(email, "access_request", reason)
    return store._rows("SELECT * FROM access_requests ORDER BY id DESC LIMIT 1")[0]


def list_requests(admin_email: str) -> list[dict]:
    _require_admin(admin_email)
    return store._rows("SELECT * FROM access_requests ORDER BY status = 'pending' DESC, id DESC LIMIT 50")


def my_requests(email: str) -> list[dict]:
    # A requester sees status only; the deal granted is not revealed beyond what their access now shows.
    return store._rows("SELECT id, created_at, reason, status FROM access_requests WHERE email = ? ORDER BY id DESC", (email,))


def decide(admin_email: str, request_id: int, approve: bool, deal_id: str | None = None) -> dict:
    _require_admin(admin_email)
    rows = store._rows("SELECT * FROM access_requests WHERE id = ?", (request_id,))
    if not rows:
        raise GovernanceError("Unknown request")
    req = rows[0]
    if req["status"] != "pending":
        raise GovernanceError("Request already decided")
    if approve:
        if not deal_id:
            raise GovernanceError("Pick the deal to staff them on.")
        set_member(admin_email, deal_id, req["email"], True)
    store.execute(
        "UPDATE access_requests SET status = ?, deal_id = ?, decided_by = ?, decided_at = ? WHERE id = ?",
        ("approved" if approve else "declined", deal_id if approve else None, admin_email, _now(), request_id),
    )
    log(admin_email, "access_decision", f"{'approved' if approve else 'declined'} request {request_id} from {req['email']}")
    return store._rows("SELECT * FROM access_requests WHERE id = ?", (request_id,))[0]


def walled_deals_mentioned(email: str, text: str) -> list[dict]:
    """Deals the user is not staffed on that their question names (codename, company or deal id).

    Used only for the audit trail; the user is never told whether a match happened.
    """
    lowered = f" {(text or '').lower()} "
    hits = []
    for deal in store._rows("SELECT id, codename, company FROM deals"):
        if store.can_access_deal(email, deal["id"]):
            continue
        names = {deal["id"], deal["codename"] or "", (deal["codename"] or "").removeprefix("Project "), deal["company"] or ""}
        if any(n and len(n) >= 4 and re.search(rf"\b{re.escape(n.lower())}\b", lowered) for n in names):
            hits.append(deal)
    return hits
