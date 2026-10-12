"""HydraDB retrieval with the caller's identity applied as ACL.

The ACL is always derived from the server-side user record, never from model or client input,
so content behind an ethical wall is never retrieved for a user outside it.
"""

from pathlib import Path

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
        if doc is None:
            # Not registered in SQLite: deleted, or not yet saved (e.g. a change log for a version
            # the demo has not saved). HydraDB deletes are async, so SQLite decides what exists.
            continue
        path, url = source_link(doc_id)
        chunks.append(
            {
                "id": doc_id,
                "title": doc["title"],
                "file": path,
                "url": url,
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


def source_link(doc_id: str) -> tuple[str | None, str | None]:
    """Where a brain document came from: (relative file path, link the UI can open)."""
    if doc_id.startswith("f_commit_"):
        commit_id = doc_id.removeprefix("f_commit_")
        rows = store._rows("SELECT file_path FROM commits WHERE id = ?", (commit_id,))
        return (_relative(rows[0]["file_path"]) if rows else None), f"#commit={commit_id}"
    if doc_id.startswith("f_model_"):
        return None, f"#model={doc_id.removeprefix('f_model_')}"
    rows = store._rows("SELECT path FROM files_seen WHERE doc_id = ?", (doc_id,))
    if not rows:
        return None, None
    return _relative(rows[0]["path"]), f"/files/{doc_id}"


def _relative(path: str | None) -> str | None:
    if not path:
        return None
    from app.ingest import IMPORT_ROOT

    try:
        return str(Path(path).resolve().relative_to(IMPORT_ROOT.resolve()))
    except ValueError:
        return path
