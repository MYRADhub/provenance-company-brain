"""HTTP API. The `user` parameter is the demo user switcher; a real deployment derives it from the session."""

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import agent, store, versioning
from app.config import STATIC_DIR

app = FastAPI(title="Provenance")


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    user: str
    messages: list[ChatMessage]


class SaveVersionRequest(BaseModel):
    user: str
    deal: str


def _require_user(email: str) -> dict:
    user = store.get_user(email)
    if not user:
        raise HTTPException(status_code=401, detail="Unknown user")
    return user


def _require_deal(email: str, deal: str) -> None:
    _require_user(email)
    if not store.can_access_deal(email, deal):
        c = store.compliance_contact() or {}
        raise HTTPException(
            status_code=403,
            detail=f"You are not cleared for this deal. Contact {c.get('name', 'Compliance')} if you believe you should be.",
        )


@app.get("/api/users")
def users():
    return store.list_users()


@app.get("/api/me")
def me(user: str):
    return {"user": _require_user(user), "deals": store.accessible_deals(user)}


@app.post("/api/chat")
def chat(req: ChatRequest):
    _require_user(req.user)
    return agent.run(req.user, [m.model_dump() for m in req.messages])


@app.get("/api/deals/{deal}/versions")
def versions(deal: str, user: str):
    _require_deal(user, deal)
    return {"deal": deal, "versions": store.list_versions(deal)}


@app.get("/api/deals/{deal}/changes")
def changes(deal: str, user: str, from_version: int | None = Query(None, alias="from"), to: int | None = None):
    _require_deal(user, deal)
    versions = [v["version"] for v in store.list_versions(deal)]
    if not versions:
        return {"changes": []}
    to = to if to is not None else max(versions)
    from_version = from_version if from_version is not None else max(min(versions), to - 1)
    return {"from": from_version, "to": to, "changes": store.get_changes(deal, from_version, to)}


@app.get("/api/deals/{deal}/stale")
def stale(deal: str, user: str):
    _require_deal(user, deal)
    return {"stale": store.stale_references(deal)}


@app.post("/api/demo/save-version")
def save_version(req: SaveVersionRequest):
    _require_deal(req.user, req.deal)
    existing = store.list_versions(req.deal)
    next_version = (max(v["version"] for v in existing) + 1) if existing else 1
    try:
        saved = versioning.save_version(req.deal, next_version)
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"No scripted version v{next_version} for this deal: {e}")
    alert = agent.proactive_alert(req.user, req.deal, saved)
    return {**saved, "alert": alert}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
