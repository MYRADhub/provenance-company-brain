"""HydraDB retrieval with the caller's identity applied as ACL.

The ACL is always derived from the server-side user record, never from model or client input,
so content behind an ethical wall is never retrieved for a user outside it.
"""

from app import store
from app.config import COLLECTION, HYDRA_DATABASE, hydra

MAX_CHUNK_CHARS = 1500


def _get(obj, name, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def search(email: str, query: str, deal: str | None = None, max_results: int = 8) -> dict:
    principals = store.user_principals(email)
    if not principals:
        return {"chunks": [], "graph_paths": []}

    kwargs = dict(
        database=HYDRA_DATABASE,
        collection=COLLECTION,
        query=query,
        type="knowledge",
        query_by="hybrid",
        mode="thinking",
        max_results=max_results,
        graph_context=True,
        acl=principals,
    )
    if deal:
        kwargs["metadata_filters"] = {"deal": deal}

    result = hydra().query(**kwargs)
    data = _get(result, "data")

    chunks = []
    for c in _get(data, "chunks", []) or []:
        meta = _get(c, "metadata", {}) or {}
        extra = _get(c, "additional_metadata", {}) or {}
        text = _get(c, "chunk_content", "") or ""
        doc_id = _get(c, "id")
        # HydraDB returns a body prefix as the title for message-kind sources, so the SQLite
        # document record is the source of truth for title, date and kind.
        doc = store.get_document(doc_id) if doc_id else None
        doc = doc or {}
        chunks.append(
            {
                "id": doc_id,
                "title": doc.get("title") or _get(c, "source_title"),
                "deal": doc.get("deal_id") or _get(meta, "deal"),
                "kind": doc.get("kind") or _get(meta, "doc_kind") or _get(c, "source_type"),
                "date": doc.get("date") or _get(extra, "date"),
                "author": doc.get("author_email") or _get(extra, "author"),
                "text": text[:MAX_CHUNK_CHARS],
            }
        )

    graph_paths = []
    graph = _get(data, "graph_context")
    for p in (_get(graph, "query_paths", []) or [])[:8]:
        graph_paths.append(p if isinstance(p, (str, dict)) else _to_plain(p))

    return {"chunks": chunks, "graph_paths": graph_paths}


def _to_plain(obj):
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    return str(obj)
