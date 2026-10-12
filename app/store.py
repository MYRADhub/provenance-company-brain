"""SQLite: the hard facts. Users, deals, walls, model versions, cell diffs, metrics."""
import json
import sqlite3
from contextlib import contextmanager

from app.config import DATA_DIR, DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(email TEXT PRIMARY KEY, name TEXT, title TEXT, role TEXT);
CREATE TABLE IF NOT EXISTS deals(id TEXT PRIMARY KEY, codename TEXT, company TEXT, sector TEXT, status TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS deal_members(deal_id TEXT, email TEXT, PRIMARY KEY(deal_id, email));
CREATE TABLE IF NOT EXISTS documents(doc_id TEXT PRIMARY KEY, title TEXT, deal_id TEXT, kind TEXT, date TEXT,
    author_email TEXT, acl_json TEXT);
CREATE TABLE IF NOT EXISTS model_versions(deal_id TEXT, version INTEGER, file_path TEXT, author_email TEXT,
    saved_at TEXT, note TEXT, PRIMARY KEY(deal_id, version));
CREATE TABLE IF NOT EXISTS cell_changes(deal_id TEXT, from_version INTEGER, to_version INTEGER, sheet TEXT,
    cell TEXT, label TEXT, old_value TEXT, new_value TEXT);
CREATE TABLE IF NOT EXISTS key_metrics(deal_id TEXT, version INTEGER, metric TEXT, value REAL,
    PRIMARY KEY(deal_id, version, metric));
CREATE TABLE IF NOT EXISTS doc_references(doc_id TEXT, doc_title TEXT, deal_id TEXT, metric TEXT,
    quoted_value REAL, quoted_version INTEGER, owner_email TEXT);
"""

TABLES = ["users", "deals", "deal_members", "documents", "model_versions", "cell_changes",
          "key_metrics", "doc_references"]


@contextmanager
def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(reset: bool = False) -> None:
    with connect() as conn:
        if reset:
            for table in TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.executescript(SCHEMA)


def _rows(sql: str, params=()) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def execute(sql: str, params=()) -> None:
    with connect() as conn:
        conn.execute(sql, params)


def executemany(sql: str, rows) -> None:
    with connect() as conn:
        conn.executemany(sql, rows)


# ----------------------------------------------------------------- people/walls

def get_user(email: str) -> dict | None:
    rows = _rows("SELECT * FROM users WHERE email = ?", (email,))
    return rows[0] if rows else None


def list_users() -> list[dict]:
    return _rows("SELECT * FROM users ORDER BY rowid")


def user_principals(email: str) -> list[str]:
    if not get_user(email):
        return []
    deals = _rows("SELECT deal_id FROM deal_members WHERE email = ? ORDER BY deal_id", (email,))
    return [email] + [f"group:northbeam:{d['deal_id']}" for d in deals]


def can_access_deal(email: str, deal_id: str) -> bool:
    return bool(_rows("SELECT 1 FROM deal_members WHERE email = ? AND deal_id = ?", (email, deal_id)))


def accessible_deals(email: str) -> list[dict]:
    return _rows(
        "SELECT d.* FROM deals d JOIN deal_members m ON m.deal_id = d.id WHERE m.email = ? ORDER BY d.rowid",
        (email,),
    )


def compliance_contact() -> dict:
    rows = _rows("SELECT * FROM users WHERE role = 'compliance' LIMIT 1")
    return rows[0] if rows else {}


def deal_team(deal_id: str) -> list[dict]:
    return _rows(
        "SELECT u.* FROM users u JOIN deal_members m ON m.email = u.email WHERE m.deal_id = ? ORDER BY u.rowid",
        (deal_id,),
    )


# ------------------------------------------------------------------- versions

def get_metrics(deal_id: str, version: int | None = None) -> dict:
    if version is None:
        version = latest_version(deal_id)
        if version is None:
            return {}
    rows = _rows("SELECT metric, value FROM key_metrics WHERE deal_id = ? AND version = ?", (deal_id, version))
    return {r["metric"]: r["value"] for r in rows}


def latest_version(deal_id: str) -> int | None:
    rows = _rows("SELECT MAX(version) AS v FROM model_versions WHERE deal_id = ?", (deal_id,))
    return rows[0]["v"] if rows and rows[0]["v"] is not None else None


def list_versions(deal_id: str) -> list[dict]:
    versions = _rows(
        "SELECT version, file_path, author_email, saved_at, note FROM model_versions WHERE deal_id = ? ORDER BY version",
        (deal_id,),
    )
    for v in versions:
        v["metrics"] = get_metrics(deal_id, v["version"])
    return versions


def _decode(value):
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def get_changes(deal_id: str, from_v: int, to_v: int) -> list[dict]:
    """Net cell-level changes from from_v to to_v, collapsing intermediate steps."""
    if from_v == to_v:
        return []
    lo, hi = sorted((from_v, to_v))
    rows = _rows(
        "SELECT * FROM cell_changes WHERE deal_id = ? AND from_version >= ? AND to_version <= ? "
        "ORDER BY to_version, rowid",
        (deal_id, lo, hi),
    )
    net: dict[tuple, dict] = {}
    for r in rows:
        key = (r["sheet"], r["cell"])
        if key not in net:
            net[key] = {"sheet": r["sheet"], "cell": r["cell"], "label": r["label"],
                        "old_value": _decode(r["old_value"]), "new_value": _decode(r["new_value"]),
                        "changed_in": [r["to_version"]]}
        else:
            net[key]["new_value"] = _decode(r["new_value"])
            net[key]["changed_in"].append(r["to_version"])
    changes = [c for c in net.values() if c["old_value"] != c["new_value"]]
    if from_v > to_v:
        for c in changes:
            c["old_value"], c["new_value"] = c["new_value"], c["old_value"]
    for c in changes:
        c["from_version"], c["to_version"] = from_v, to_v
    return changes


def stale_references(deal_id: str) -> list[dict]:
    latest = latest_version(deal_id)
    if latest is None:
        return []
    metrics = get_metrics(deal_id, latest)
    refs = _rows("SELECT * FROM doc_references WHERE deal_id = ? ORDER BY doc_id, rowid", (deal_id,))
    stale = []
    for r in refs:
        current = metrics.get(r["metric"])
        if current is None or abs(current - r["quoted_value"]) < 1e-6:
            continue
        stale.append({**r, "latest_value": current, "latest_version": latest})
    return stale


def get_document(doc_id: str) -> dict | None:
    rows = _rows("SELECT * FROM documents WHERE doc_id = ?", (doc_id,))
    if not rows:
        return None
    doc = rows[0]
    doc["acl"] = json.loads(doc.pop("acl_json") or "[]")
    return doc
