"""Where the brain's documents are indexed and searched: HydraDB (hosted) or a local SQLite full-text index.

The local index keeps every document on this machine: nothing leaves it to be searched. It is the default
whenever no HydraDB key is configured, and can be forced with BRAIN_RETRIEVAL=local (or =hydra).

Local search is keyword search (SQLite FTS5, BM25 ranking, Porter stemming): no embeddings and no graph
context, so "why did growth change" finds documents that use those words, not ones that only mean them.
The ACL check is the same rule HydraDB applies: a chunk is returned only when one of its principals is
the user, a deal group the user is staffed on, or the user's email domain (firm-wide documents).
"""

import json
import os
import re

from app import store

CHUNK_CHARS = 1500
CHUNK_OVERLAP = 200
# Candidates read before the ACL filter; generous so walled matches cannot crowd out readable ones.
CANDIDATES = 400
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "did", "do", "does", "for", "from", "has", "have", "how",
    "in", "is", "it", "its", "of", "on", "or", "that", "the", "this", "to", "was", "were", "what", "when",
    "where", "which", "who", "why", "will", "with",
}


def backend() -> str:
    chosen = os.environ.get("BRAIN_RETRIEVAL", "").strip().lower()
    if chosen in ("local", "hydra"):
        return chosen
    return "hydra" if os.environ.get("HYDRA_DB_API_KEY") else "local"


def chunks_of(body: str) -> list[str]:
    """Split on paragraph boundaries into pieces of about CHUNK_CHARS, overlapping a little."""
    body = body.strip()
    if len(body) <= CHUNK_CHARS:
        return [body] if body else []
    out, start = [], 0
    while start < len(body):
        end = min(len(body), start + CHUNK_CHARS)
        if end < len(body):
            cut = body.rfind("\n\n", start + CHUNK_CHARS // 2, end)
            if cut == -1:
                cut = body.rfind(" ", start + CHUNK_CHARS // 2, end)
            end = cut if cut != -1 else end
        out.append(body[start:end].strip())
        if end >= len(body):
            break
        start = max(end - CHUNK_OVERLAP, start + 1)
    return [c for c in out if c]


def index_local(docs: list[dict]) -> None:
    """Add or replace documents in the local index (same document dicts as ingest builds for HydraDB)."""
    with store.connect() as conn:
        for doc in docs:
            conn.execute("DELETE FROM search_chunks WHERE doc_id = ?", (doc["doc_id"],))
            conn.executemany(
                "INSERT INTO search_chunks(doc_id, deal, acl, title, body) VALUES (?,?,?,?,?)",
                [(doc["doc_id"], doc.get("deal_id") or "firm", json.dumps(doc.get("acl") or []), doc["title"], piece)
                 for piece in chunks_of(doc["body"])],
            )


def remove_local(doc_ids: list[str]) -> None:
    with store.connect() as conn:
        conn.executemany("DELETE FROM search_chunks WHERE doc_id = ?", [(d,) for d in doc_ids])


def match_expression(query: str) -> str | None:
    """An FTS5 query from free text: every meaningful word, any of them, each quoted so no syntax leaks in."""
    words = [w for w in re.findall(r"[\w']+", query.lower()) if w not in STOPWORDS and len(w) > 1]
    if not words:
        words = re.findall(r"[\w']+", query.lower())
    words = list(dict.fromkeys(w.replace('"', "") for w in words))[:24]
    return " OR ".join(f'"{w}"' for w in words) if words else None


def allowed_principals(email: str) -> set[str]:
    principals = store.user_principals(email)
    if not principals:
        return set()
    domain = email.rsplit("@", 1)[-1].lower()
    return set(principals) | {f"domain:{domain}"}


def search_local(email: str, query: str, deal: str | None, max_results: int) -> list[dict]:
    """Ranked chunks the user may read: [{doc_id, title, deal, text}]."""
    allowed = allowed_principals(email)
    expression = match_expression(query)
    if not allowed or not expression:
        return []
    sql = "SELECT doc_id, deal, acl, title, body FROM search_chunks WHERE search_chunks MATCH ?"
    params: list = [expression]
    if deal:
        sql += " AND deal = ?"
        params.append(deal)
    sql += " ORDER BY bm25(search_chunks, 0, 0, 0, 2.0, 1.0) LIMIT ?"
    params.append(CANDIDATES)
    out = []
    for row in store._rows(sql, tuple(params)):
        if not allowed.intersection(json.loads(row["acl"] or "[]")):
            continue
        out.append({"doc_id": row["doc_id"], "title": row["title"], "deal": row["deal"], "text": row["body"]})
        if len(out) >= max_results:
            break
    return out
