"""SQLite: the hard facts. Users, deals, walls, documents; model history tables live here too (see app/vcs.py)."""
import json
import sqlite3
from contextlib import contextmanager

from app import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(email TEXT PRIMARY KEY, name TEXT, title TEXT, role TEXT);
CREATE TABLE IF NOT EXISTS deals(id TEXT PRIMARY KEY, codename TEXT, company TEXT, sector TEXT, status TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS deal_members(deal_id TEXT, email TEXT, PRIMARY KEY(deal_id, email));
CREATE TABLE IF NOT EXISTS documents(doc_id TEXT PRIMARY KEY, title TEXT, deal_id TEXT, kind TEXT, date TEXT,
    author_email TEXT, acl_json TEXT);
CREATE TABLE IF NOT EXISTS models(id TEXT PRIMARY KEY, deal_id TEXT, name TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS commits(id TEXT PRIMARY KEY, model_id TEXT, branch TEXT, parent_id TEXT,
    author_email TEXT, committed_at TEXT, message TEXT, file_path TEXT, file_mtime REAL);
CREATE TABLE IF NOT EXISTS branches(model_id TEXT, name TEXT, head_commit TEXT, file_path TEXT,
    PRIMARY KEY(model_id, name));
CREATE TABLE IF NOT EXISTS cell_changes(commit_id TEXT, sheet TEXT, cell TEXT, label TEXT, old_value TEXT,
    new_value TEXT, old_formula TEXT, new_formula TEXT);
CREATE TABLE IF NOT EXISTS model_metric_cells(model_id TEXT, metric TEXT, sheet TEXT, cell TEXT,
    PRIMARY KEY(model_id, metric));
CREATE TABLE IF NOT EXISTS commit_metrics(commit_id TEXT, metric TEXT, value REAL, PRIMARY KEY(commit_id, metric));
CREATE TABLE IF NOT EXISTS doc_references(doc_id TEXT, doc_title TEXT, deal_id TEXT, model_id TEXT, metric TEXT,
    quoted_value REAL, quoted_commit TEXT, owner_email TEXT);
CREATE TABLE IF NOT EXISTS files_seen(path TEXT PRIMARY KEY, sha256 TEXT, mtime REAL, kind TEXT, doc_id TEXT,
    commit_id TEXT);
CREATE TABLE IF NOT EXISTS access_log(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, email TEXT, action TEXT,
    detail TEXT, outcome TEXT);
CREATE TABLE IF NOT EXISTS access_requests(id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, email TEXT,
    reason TEXT, status TEXT, deal_id TEXT, decided_by TEXT, decided_at TEXT);
CREATE TABLE IF NOT EXISTS api_tokens(token_hash TEXT PRIMARY KEY, email TEXT, label TEXT, created_at TEXT,
    last_used_at TEXT);
"""

TABLES = ["users", "deals", "deal_members", "documents", "models", "commits", "branches", "cell_changes",
          "model_metric_cells", "commit_metrics", "doc_references", "files_seen", "access_log", "access_requests",
          "model_versions", "key_metrics", "api_tokens"]


@contextmanager
def connect():
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH)
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


def get_document(doc_id: str) -> dict | None:
    rows = _rows("SELECT * FROM documents WHERE doc_id = ?", (doc_id,))
    if not rows:
        return None
    doc = rows[0]
    doc["acl"] = json.loads(doc.pop("acl_json") or "[]")
    return doc
