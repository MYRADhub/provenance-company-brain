"""HTTP API. The `user` parameter is the demo user switcher; a real deployment derives it from the session."""

import json
import shutil
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import agent, api_v1, auth, governance, ingest, mcp, store, vcs, watcher
from app.config import STATIC_DIR


@asynccontextmanager
async def lifespan(_app: FastAPI):
    store.init_db()
    watcher.start(ingest.IMPORT_ROOT)
    yield
    watcher.stop()


app = FastAPI(title="Provenance", lifespan=lifespan)
app.include_router(mcp.router)
app.include_router(api_v1.router)

# Open without a credential even when sign-in is required: signing in, and asking whether it is required.
OPEN_PATHS = {"/api/session", "/api/auth-config"}


@app.middleware("http")
async def identity_gate(request: Request, call_next):
    """With BRAIN_REQUIRE_AUTH, the web UI's API answers only a signed-in caller, and only about themselves.

    /api/v1 and /mcp authenticate every request themselves.
    """
    path = request.url.path
    gated = path.startswith(("/api/", "/files/")) and not path.startswith("/api/v1/") and path not in OPEN_PATHS
    if not auth.require_auth() or not gated or request.method == "OPTIONS":
        return await call_next(request)
    email = auth.from_header(request.headers.get("authorization")) or auth.user_for_cookie(request.cookies.get(auth.COOKIE))
    if not email:
        return JSONResponse({"detail": "Sign in first."}, status_code=401)
    claimed = request.query_params.get("user")
    if claimed is None and request.method == "POST":
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            body = {}
        claimed = body.get("user") if isinstance(body, dict) else None
    if claimed is not None and claimed.lower() != email.lower():
        governance.log(email, "blocked", f"acted as {claimed} on {path}", "blocked")
        return JSONResponse({"detail": "You can only act as yourself."}, status_code=403)
    request.state.user = email
    return await call_next(request)


class SessionRequest(BaseModel):
    token: str


@app.get("/api/auth-config")
def auth_config():
    return {"require_auth": auth.require_auth(), "mode": auth.mode()}


@app.post("/api/session")
def start_session(req: SessionRequest, request: Request, response: Response):
    """Sign the web UI in with an API token or a sign-in token; sets an HttpOnly session cookie."""
    email = auth.user_for_token(req.token)
    if not email:
        raise HTTPException(status_code=401, detail="That token was not accepted.")
    response.set_cookie(auth.COOKIE, auth.session_cookie(email), max_age=auth.SESSION_SECONDS, httponly=True,
                        samesite="strict", secure=request.url.scheme == "https")
    governance.log(email, "sign_in", "web session")
    return {"user": store.get_user(email)}


@app.delete("/api/session")
def end_session(response: Response):
    response.delete_cookie(auth.COOKIE)
    return {"ok": True}
# Tessel's browser build calls /mcp and /api/v1 from its own origin; the desktop app sends none.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=mcp.LOCAL_ORIGIN.pattern,
    allow_origins=mcp.extra_origins(),
    allow_methods=["GET", "POST"],
    allow_headers=["authorization", "content-type", "mcp-protocol-version", "mcp-session-id"],
)


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
def users(request: Request):
    # The demo user switcher lists everyone; with sign-in required it lists only the caller.
    if auth.require_auth():
        return [store.get_user(request.state.user)]
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
_alert_started: dict[tuple[str, str], float] = {}
_alert_floor = 0


def _latest_seq() -> int:
    evs = watcher.events_since(0)
    return max((e["seq"] for e in evs), default=0)


ALERT_STALE_S = 150  # a job pending longer than this is presumed hung and is started again


def _alert_for(email: str, commit: dict) -> dict | None:
    key = (commit["id"], email)
    now = time.monotonic()
    with _alerts_lock:
        if key in _alerts:
            if _alerts[key] is not None:
                return _alerts[key]
            if now - _alert_started.get(key, now) < ALERT_STALE_S:
                return None
        _alerts[key] = None
        _alert_started[key] = now

    def work():
        try:
            result = agent.proactive_alert(email, commit)
        except Exception as e:
            result = {"answer": f"Could not generate the alert: {e}", "tool_trace": [], "citations": []}
        with _alerts_lock:
            if _alert_started.get(key) == now:  # ignore a late result from a job that was restarted
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


@app.get("/files/{doc_id}")
def source_file(doc_id: str, user: str):
    """Open the original file behind a citation. Walls apply exactly as in search."""
    _require_user(user)
    doc = store.get_document(doc_id)
    rows = store._rows("SELECT path FROM files_seen WHERE doc_id = ?", (doc_id,))
    if not doc or not rows:
        raise HTTPException(status_code=404, detail="Not found")
    if doc.get("deal_id") and doc["deal_id"] != "firm" and not store.can_access_deal(user, doc["deal_id"]):
        governance.log(user, "blocked", f"open file {doc_id}", "blocked")
        raise HTTPException(status_code=404, detail="Not found")
    path = Path(rows[0]["path"])
    path = path if path.is_absolute() else ingest.IMPORT_ROOT / path
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File no longer on disk")
    inline = path.suffix.lower() in (".pdf", ".txt", ".md", ".eml")
    media = {".txt": "text/plain; charset=utf-8", ".md": "text/plain; charset=utf-8", ".eml": "text/plain; charset=utf-8"}.get(path.suffix.lower())
    governance.log(user, "open_file", doc["title"])
    return FileResponse(path, media_type=media, filename=path.name,
                        content_disposition_type="inline" if inline else "attachment")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
