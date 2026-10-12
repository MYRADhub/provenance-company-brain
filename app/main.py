"""HTTP API. The `user` parameter is the demo user switcher; a real deployment derives it from the session."""

import shutil
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import agent, governance, ingest, store, vcs, watcher
from app.config import STATIC_DIR


@asynccontextmanager
async def lifespan(_app: FastAPI):
    store.init_db()
    watcher.start(ingest.IMPORT_ROOT)
    yield
    watcher.stop()


app = FastAPI(title="Provenance", lifespan=lifespan)


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    user: str
    messages: list[ChatMessage]


class ImportRequest(BaseModel):
    user: str
    path: str | None = None


class StaffingRequest(BaseModel):
    user: str
    deal: str
    member: str
    staffed: bool


class AccessRequest(BaseModel):
    user: str
    reason: str


class DecisionRequest(BaseModel):
    user: str
    approve: bool
    deal: str | None = None


class DemoSaveRequest(BaseModel):
    user: str
    deal: str


def _require_user(email: str) -> dict:
    user = store.get_user(email)
    if not user:
        raise HTTPException(status_code=401, detail="Unknown user")
    return user


def _forbidden() -> HTTPException:
    c = store.compliance_contact() or {}
    return HTTPException(
        status_code=403,
        detail=f"You are not cleared for this deal. Contact {c.get('name', 'Compliance')} if you believe you should be.",
    )


def _require_deal(email: str, deal: str) -> None:
    _require_user(email)
    if not store.can_access_deal(email, deal):
        raise _forbidden()


def _require_model(email: str, model_id: str) -> dict:
    _require_user(email)
    for d in store.accessible_deals(email):
        for m in vcs.list_models(d["id"]):
            if m["id"] == model_id:
                return m
    raise _forbidden()


def _require_commit(email: str, commit_id: str) -> dict:
    commit = vcs.get_commit(commit_id)
    if not commit:
        raise _forbidden()
    _require_model(email, commit["model_id"])
    return commit


@app.get("/api/users")
def users():
    return store.list_users()


@app.get("/api/me")
def me(user: str):
    return {"user": _require_user(user), "deals": store.accessible_deals(user), "can_manage_access": governance.is_admin(user)}


@app.post("/api/chat")
def chat(req: ChatRequest):
    _require_user(req.user)
    return agent.run(req.user, [m.model_dump() for m in req.messages])


@app.get("/api/deals/{deal}/models")
def models(deal: str, user: str):
    _require_deal(user, deal)
    return {"models": vcs.list_models(deal)}


@app.get("/api/models/{model_id}/graph")
def model_graph(model_id: str, user: str):
    _require_model(user, model_id)
    return vcs.graph(model_id)


@app.get("/api/commits/diff")
def commits_diff(user: str, a: str, b: str):
    ca, cb = _require_commit(user, a), _require_commit(user, b)
    if ca["model_id"] != cb["model_id"]:
        raise HTTPException(status_code=400, detail="Commits belong to different models")
    return {"from": a, "to": b, "changes": vcs.commit_diff(a, b)}


@app.get("/api/deals/{deal}/stale")
def stale(deal: str, user: str):
    _require_deal(user, deal)
    return {"stale": vcs.stale_references(deal)}


@app.post("/api/import")
def import_folder(req: ImportRequest):
    user = _require_user(req.user)
    if user["role"] not in ("managing_partner", "compliance", "principal"):
        raise HTTPException(status_code=403, detail="Only principals, partners and compliance can import folders.")
    root = Path(req.path).expanduser() if req.path else ingest.IMPORT_ROOT
    if not root.is_dir():
        raise HTTPException(status_code=400, detail=f"Not a folder: {root}")
    global _alert_floor
    result = ingest.scan(root)
    _alert_floor = max(_alert_floor, _latest_seq())
    return {
        "root": str(root),
        "commits": len(result.get("commits", [])),
        "docs": len(result.get("docs", [])),
        "skipped": result.get("skipped", 0),
    }


@app.post("/api/demo/save")
def demo_save(req: DemoSaveRequest):
    _require_deal(req.user, req.deal)
    for src in sorted(p for p in ingest.LIVE_ROOT.rglob("*") if p.is_file()):
        rel = src.relative_to(ingest.LIVE_ROOT)
        dest = ingest.IMPORT_ROOT / rel
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            dest.touch()  # a save happens now
            return {"saved": str(rel)}
    raise HTTPException(status_code=400, detail="No more scripted saves. Edit and save a model in Excel instead.")


def _gov(fn, *args):
    try:
        return fn(*args)
    except governance.GovernanceError as e:
        status = 403 if "Only Compliance" in str(e) else 400
        raise HTTPException(status_code=status, detail=str(e))


@app.get("/api/governance")
def governance_overview(user: str):
    _require_user(user)
    return _gov(governance.overview, user)


@app.post("/api/governance/staffing")
def governance_staffing(req: StaffingRequest):
    _require_user(req.user)
    _gov(governance.set_member, req.user, req.deal, req.member, req.staffed)
    return _gov(governance.overview, req.user)


@app.post("/api/access-requests")
def create_access_request(req: AccessRequest):
    _require_user(req.user)
    return _gov(governance.request_access, req.user, req.reason)


@app.get("/api/access-requests/mine")
def my_access_requests(user: str):
    _require_user(user)
    return {"requests": governance.my_requests(user)}


@app.post("/api/access-requests/{request_id}/decide")
def decide_access_request(request_id: int, req: DecisionRequest):
    _require_user(req.user)
    return _gov(governance.decide, req.user, request_id, req.approve, req.deal)


# Proactive alerts are generated per (commit, user) because each user sees the brain through their own walls.
# Only live saves get alerts: events at or below _alert_floor came from bulk imports.
_alerts: dict[tuple[str, str], dict | None] = {}
_alerts_lock = threading.Lock()
_alert_floor = 0


def _latest_seq() -> int:
    evs = watcher.events_since(0)
    return max((e["seq"] for e in evs), default=0)


def _alert_for(email: str, commit: dict) -> dict | None:
    key = (commit["id"], email)
    with _alerts_lock:
        if key in _alerts:
            return _alerts[key]
        _alerts[key] = None

    def work():
        try:
            result = agent.proactive_alert(email, commit)
        except Exception as e:
            result = {"answer": f"Could not generate the alert: {e}", "tool_trace": [], "citations": []}
        with _alerts_lock:
            _alerts[key] = result

    threading.Thread(target=work, daemon=True).start()
    return None


@app.get("/api/events")
def events(user: str, since: int = 0):
    _require_user(user)
    out, seq = [], since
    for ev in watcher.events_since(since):
        seq = max(seq, ev["seq"])
        deal_id = ev.get("deal_id")
        if deal_id and deal_id != "firm" and not store.can_access_deal(user, deal_id):
            continue
        item = {k: v for k, v in ev.items() if k != "commit"}
        if ev.get("type") == "commit" and ev.get("commit"):
            item["commit"] = {k: v for k, v in ev["commit"].items() if k != "changes"}
            # since=0 is the client priming its cursor: history, not news.
            live = since > 0 and ev["seq"] > _alert_floor
            item["alert"] = _alert_for(user, ev["commit"]) if live else None
            item["alert_pending"] = live and item["alert"] is None
        out.append(item)
    return {"events": out, "seq": seq}


@app.get("/api/alerts/{commit_id}")
def alert(commit_id: str, user: str):
    commit = _require_commit(user, commit_id)
    with _alerts_lock:
        known = (commit_id, user) in _alerts
        result = _alerts.get((commit_id, user))
    if not known:
        full = next((ev["commit"] for ev in watcher.events_since(0) if ev.get("commit", {}).get("id") == commit_id), None)
        result = _alert_for(user, full or commit)
    return {"pending": result is None, "alert": result}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
