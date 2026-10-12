"""Background folder watcher: rescans the import root every few seconds and queues events.

Events are kept in memory: {seq, type: "commit"|"doc", deal_id, ...}. Commit events carry the full
record_snapshot dict under "commit". Consumers poll events_since(seq).
"""

import threading
from pathlib import Path

from app import ingest

_lock = threading.Lock()
_events: list[dict] = []
_seq = 0
_thread: threading.Thread | None = None
_stop = threading.Event()
_last_error: str | None = None


def _push(event: dict) -> None:
    global _seq
    with _lock:
        _seq += 1
        _events.append({**event, "seq": _seq})


def publish(result: dict) -> None:
    """Queue events for a scan result (also used by callers that run ingest.scan themselves)."""
    for commit in result.get("commits", []):
        _push({"type": "commit", "deal_id": commit.get("deal_id"), "commit": commit})
    for doc in result.get("docs", []):
        _push({"type": "doc", "deal_id": doc.get("deal_id") or "firm", "doc_id": doc["doc_id"],
               "title": doc["title"], "doc_kind": doc["doc_kind"], "path": doc.get("path")})


def scan_once(root: Path | str) -> dict:
    global _last_error
    try:
        result = ingest.scan(root)
    except Exception as e:  # the watcher must survive a bad file or a network blip
        _last_error = f"{type(e).__name__}: {e}"
        return {"commits": [], "docs": [], "errors": [{"path": "*", "error": _last_error}]}
    if result.get("errors"):
        _last_error = "; ".join(f"{e['path']}: {e['error']}" for e in result["errors"])[:500]
    publish(result)
    return result


def _loop(root: Path, interval: float) -> None:
    while not _stop.is_set():
        if root.is_dir():
            scan_once(root)
        _stop.wait(interval)


def start(root: Path | str = ingest.IMPORT_ROOT, interval: float = 3.0) -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(Path(root), interval), name="provenance-watcher", daemon=True)
    _thread.start()


def stop(timeout: float = 5.0) -> None:
    _stop.set()
    if _thread:
        _thread.join(timeout)


def events_since(seq: int) -> list[dict]:
    with _lock:
        return [dict(e) for e in _events if e["seq"] > seq]


def latest_seq() -> int:
    with _lock:
        return _seq


def status() -> dict:
    return {"running": bool(_thread and _thread.is_alive()), "seq": latest_seq(), "last_error": _last_error}

