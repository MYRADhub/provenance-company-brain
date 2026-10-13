"""The API apps use (Tessel first): who am I, and record a save.

Token auth as for MCP (app/tokens.py). A save sent here becomes a commit straight away, with the caller as
author, instead of waiting for the folder watcher and guessing the author from the folder.
"""

import base64
import binascii
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ValidationError

from app import config, governance, ingest, mcp, retrieval, store, tokens, vcs, watcher

router = APIRouter(prefix="/api/v1")

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
WORKBOOK_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
UNSAFE_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _caller(authorization: str | None, origin: str | None) -> str:
    if not mcp.allowed_origin(origin):
        raise HTTPException(status_code=403, detail="Origin not allowed")
    email = tokens.from_header(authorization)
    if not email:
        raise HTTPException(status_code=401, detail="A valid bearer token is required.",
                            headers={"WWW-Authenticate": 'Bearer realm="provenance"'})
    return email


def _not_found() -> HTTPException:
    # Same answer for a walled deal and one that does not exist.
    return HTTPException(status_code=404, detail="No such deal, or you are not staffed on it.")


@router.get("/me")
def me(authorization: str | None = Header(None), origin: str | None = Header(None)):
    email = _caller(authorization, origin)
    user = store.get_user(email) or {}
    deals = [{"id": d["id"], "codename": d["codename"], "company": d["company"], "status": d["status"]}
             for d in store.accessible_deals(email)]
    # Where document search runs, so the app can tell its user what leaves the firm.
    return {"user": {"email": email, "name": user.get("name"), "title": user.get("title")}, "deals": deals,
            "retrieval": retrieval.backend()}


class SaveRequest(BaseModel):
    deal: str
    # Stable id the app keeps inside the workbook, so a file keeps its branch wherever it is saved.
    workbook_id: str
    file_name: str
    # The .xlsx/.xlsm bytes, base64.
    data: str
    # Milliseconds since the epoch; defaults to now.
    saved_at: float | None = None


def _safe_file_name(name: str) -> str:
    name = UNSAFE_NAME.sub("_", Path(name.replace("\\", "/")).name).strip(" .")
    if Path(name).suffix.lower() not in ingest.EXCEL_EXT:
        raise HTTPException(status_code=400, detail="Only .xlsx and .xlsm files can be recorded.")
    return name[:120]


def _commit_summary(commit: dict) -> dict:
    return {
        "id": commit["id"],
        "model": commit["model"],
        "branch": commit["branch"],
        "parent_id": commit["parent_id"],
        "is_fork": commit["is_fork"],
        "message": commit["message"],
        "committed_at": commit["committed_at"],
        "changed_cells": len(commit.get("changes") or []),
        "metrics": commit.get("metrics") or {},
        "prior_metrics": commit.get("prior_metrics") or {},
    }


def _stale_for(deal_id: str, model_id: str) -> list[dict]:
    owners = {u["email"]: u["name"] for u in store.list_users()}
    return [
        {"document": r["doc_title"], "metric": r["metric"], "quoted_value": r["quoted_value"],
         "latest_value": r["latest_value"], "owner": owners.get(r["owner_email"], r["owner_email"])}
        for r in vcs.stale_references(deal_id) if r["model_id"] == model_id
    ]


@router.post("/saves")
async def record_save(request: Request):
    # Who is asking comes first: an unauthenticated caller learns nothing about the body we expect.
    email = _caller(request.headers.get("authorization"), request.headers.get("origin"))
    try:
        req = SaveRequest.model_validate(await request.json())
    except (ValueError, ValidationError):
        raise HTTPException(status_code=400, detail="Send JSON with deal, workbook_id, file_name and data.")
    return await run_in_threadpool(_record, email, req)


def _record(email: str, req: SaveRequest) -> dict:
    if not store.can_access_deal(email, req.deal):
        governance.log(email, "blocked", f"save to deal {req.deal}", "blocked")
        raise _not_found()
    if not WORKBOOK_ID.match(req.workbook_id):
        raise HTTPException(status_code=400, detail="workbook_id must be 8 to 64 letters, digits, - or _.")
    name = _safe_file_name(req.file_name)
    if len(req.data) > MAX_UPLOAD_BYTES * 4 // 3 + 4:
        raise HTTPException(status_code=413, detail="The file is larger than 25 MB.")
    try:
        data = base64.b64decode(req.data, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="data is not valid base64.")
    if not data.startswith(b"PK"):
        raise HTTPException(status_code=400, detail="data is not an Excel workbook.")

    saved_ms = req.saved_at if req.saved_at is not None else datetime.now(timezone.utc).timestamp() * 1000
    path = (config.DATA_DIR / "uploads" / req.deal / req.workbook_id / name).resolve()
    try:
        commit, warnings = ingest.record_upload(path, data, saved_ms, req.deal, email)
    except Exception as e:
        governance.log(email, "save", f"{name} failed: {e}", "error")
        raise HTTPException(status_code=422, detail=f"The workbook could not be read: {type(e).__name__}: {e}")
    governance.log(email, "save", f"{req.deal}/{name}" + ("" if commit else " (unchanged)"))
    if commit is None:
        return {"commit": None, "stale": [], "warnings": warnings}
    watcher.publish({"commits": [commit]})
    return {"commit": _commit_summary(commit), "stale": _stale_for(req.deal, commit["model_id"]), "warnings": warnings}


HISTORY_LIMIT = 50
CELL_REF = re.compile(r"^[A-Z]{1,3}[1-9][0-9]{0,6}$")


def _lineage(deal_id: str, workbook_id: str) -> list[dict]:
    """The workbook's versions, newest first: its latest commit and every ancestor (which may predate the app's
    first save, when the workbook started as a scanned file)."""
    folder = str((config.DATA_DIR / "uploads" / deal_id / workbook_id).resolve()) + os.sep
    latest = store._rows(
        "SELECT c.id FROM commits c JOIN models m ON m.id = c.model_id "
        "WHERE m.deal_id = ? AND substr(c.file_path, 1, ?) = ? ORDER BY c.committed_at DESC, c.rowid DESC LIMIT 1",
        (deal_id, len(folder), folder),
    )
    out: list[dict] = []
    commit_id = latest[0]["id"] if latest else None
    seen: set[str] = set()
    while commit_id and commit_id not in seen and len(out) < HISTORY_LIMIT:
        seen.add(commit_id)
        rows = store._rows("SELECT * FROM commits WHERE id = ?", (commit_id,))
        if not rows:
            break
        out.append(rows[0])
        commit_id = rows[0]["parent_id"]
    return out


@router.get("/history")
def history(deal: str, workbook_id: str, sheet: str | None = None, cell: str | None = None,
            authorization: str | None = Header(None), origin: str | None = Header(None)):
    """A linked workbook's saved versions, and with sheet and cell, how that cell's value changed across them."""
    email = _caller(authorization, origin)
    if not store.can_access_deal(email, deal):
        governance.log(email, "blocked", f"history of deal {deal}", "blocked")
        raise _not_found()
    if not WORKBOOK_ID.match(workbook_id):
        raise HTTPException(status_code=400, detail="workbook_id must be 8 to 64 letters, digits, - or _.")
    if (sheet is None) != (cell is None) or (cell is not None and not CELL_REF.match(cell)):
        raise HTTPException(status_code=400, detail="Give both sheet and cell (like C7), or neither.")

    names = {u["email"]: u["name"] for u in store.list_users()}
    lineage = _lineage(deal, workbook_id)
    versions = [
        {"commit": c["id"], "committed_at": c["committed_at"], "author": names.get(c["author_email"], c["author_email"]),
         "message": c["message"], "branch": c["branch"]}
        for c in lineage
    ]
    result: dict = {"versions": versions}
    if cell is not None:
        # Oldest first, keeping each version where the cell's value or formula changed.
        changes: list[dict] = []
        label = ""
        for c in reversed(lineage):
            try:
                found = vcs.load_snapshot(c["id"])["sheets"].get(sheet, {}).get(cell)
            except (OSError, ValueError):
                continue
            value, formula = (found["value"], found["formula"]) if found else (None, None)
            label = (found or {}).get("label") or label
            if changes and changes[-1]["value"] == value and changes[-1]["formula"] == formula:
                continue
            if not changes and value is None and formula is None:
                continue
            changes.append({"commit": c["id"], "committed_at": c["committed_at"],
                            "author": names.get(c["author_email"], c["author_email"]), "value": value, "formula": formula})
        result["cell"] = {"sheet": sheet, "cell": cell, "label": label, "changes": changes}
    return result
