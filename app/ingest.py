"""Import a folder of deal files into the brain.

Folder convention (what a fund's shared drive usually looks like):

    <root>/<Deal>/...        everything under a deal folder belongs to that deal's team
    <root>/<Deal>/deal.json  optional: codename, company, sector, status, summary, lead, team[]
    <root>/_firm/...         firm-wide knowledge, visible to everyone at the firm
    _authors.json            optional, in any folder: {file name: author email}

Excel files become commits in a model's history (app.vcs). Everything else is turned into text and
sent to HydraDB with the deal's ACL. Every brain document is registered in SQLite `documents`,
which is what app.brain trusts. Rescans are incremental: files whose sha256 is unchanged are skipped.
"""

import email
import email.policy
import fcntl
import hashlib
import json
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path

from app import config, store
from app.config import COLLECTION, HYDRA_DATABASE, LLM_MODEL, ROOT, hydra

IMPORT_ROOT = ROOT / "samples" / "Northbeam Deals"
LIVE_ROOT = ROOT / "samples" / "_live"

FIRM_FOLDER = "_firm"
DOMAIN = "northbeam.com"
FIRM_ACL = [f"domain:{DOMAIN}"]
EXCEL_EXT = {".xlsx", ".xlsm"}
TEXT_EXT = {".txt", ".md", ".eml", ".docx", ".pdf"}
QUOTING_KINDS = {"ic_memo", "deck", "model_brief"}
METRICS = ["revenue_growth", "ebitda_margin", "fy26_revenue", "fy26_ebitda", "exit_multiple", "irr", "moic"]
PERCENT_METRICS = {"revenue_growth", "ebitda_margin", "irr"}
METRIC_LABELS = {
    "revenue_growth": "Revenue growth", "ebitda_margin": "EBITDA margin", "fy26_revenue": "FY26 revenue",
    "fy26_ebitda": "FY26 EBITDA", "exit_multiple": "Exit multiple", "irr": "Gross IRR", "moic": "MOIC",
}
HYDRA_KIND = {"transcript": "meeting", "email": "email", "chat": "message"}
BATCH = 10

_scan_lock = threading.Lock()


# --------------------------------------------------------------------------- small helpers

def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def doc_id_for(rel_path: str) -> str:
    """Stable id per file path, prefixed so it never collides with hand-seeded ids."""
    return "f_" + hashlib.sha1(rel_path.encode("utf-8")).hexdigest()[:12]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def is_ignored(path: Path) -> bool:
    name = path.name
    return name.startswith((".", "_", "~$")) or name == "deal.json"


def deal_acl(deal_id: str | None) -> list[str]:
    return FIRM_ACL if deal_id is None else [f"group:northbeam:{deal_id}"]


def iso_from_mtime(mtime: float) -> str:
    return datetime.fromtimestamp(mtime).isoformat(timespec="seconds")


def _fmt_metric(metric: str, value) -> str:
    if value is None:
        return "n/a"
    if metric in PERCENT_METRICS:
        return f"{value * 100:.1f}%"
    if metric in ("exit_multiple", "moic"):
        return f"{value:.2f}x" if metric == "moic" else f"{value:.1f}x"
    return f"${value:.2f}M"


# --------------------------------------------------------------------------- deals and authors

def resolve_deal(path: Path, root: Path) -> tuple[str | None, Path | None]:
    """(deal_id, deal_dir) from the first folder level under root. `_firm` and loose files are firm-wide."""
    rel = path.relative_to(root)
    if len(rel.parts) < 2 or rel.parts[0] == FIRM_FOLDER:
        return None, (root / FIRM_FOLDER if rel.parts[0] == FIRM_FOLDER else None)
    return slug(rel.parts[0]), root / rel.parts[0]


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def ensure_deal(deal_id: str, deal_dir: Path) -> dict:
    """Create or refresh the deal row and its team from deal.json (folder name when absent)."""
    info = _read_json(deal_dir / "deal.json")
    folder = deal_dir.name
    row = {
        "id": deal_id,
        "codename": info.get("codename") or f"Project {folder}",
        "company": info.get("company") or folder,
        "sector": info.get("sector") or "",
        "status": info.get("status") or "active",
        "summary": info.get("summary") or "",
    }
    store.execute(
        "INSERT OR REPLACE INTO deals(id, codename, company, sector, status, summary) VALUES (?,?,?,?,?,?)",
        tuple(row.values()),
    )
    team = [e for e in info.get("team", []) if isinstance(e, str)]
    # deal.json seeds the team only when the deal is new or the file itself changed, so staffing
    # changes made in the governance screen survive every rescan.
    key = f"{folder}/deal.json"
    raw = (deal_dir / "deal.json").read_bytes() if (deal_dir / "deal.json").is_file() else b""
    sha = hashlib.sha256(raw).hexdigest()
    seen = store._rows("SELECT sha256 FROM files_seen WHERE path = ?", (key,))
    if team and (not seen or seen[0]["sha256"] != sha):
        store.execute("DELETE FROM deal_members WHERE deal_id = ?", (deal_id,))
        store.executemany("INSERT OR IGNORE INTO deal_members(deal_id, email) VALUES (?,?)",
                          [(deal_id, e) for e in team])
    if not seen or seen[0]["sha256"] != sha:
        store.execute("INSERT OR REPLACE INTO files_seen(path, sha256, mtime, kind, doc_id, commit_id) VALUES (?,?,?,?,?,?)",
                      (key, sha, time.time(), "deal_json", None, None))
    return {**row, "lead": info.get("lead"), "team": team}


def resolve_author(path: Path, stop_dir: Path | None, deal_info: dict | None) -> str:
    """Nearest _authors.json entry for this file name, else the deal lead, else a team member."""
    d = path.parent
    while True:
        hint = _read_json(d / "_authors.json").get(path.name)
        if hint:
            return hint
        if stop_dir is None or d == stop_dir or d == d.parent:
            break
        d = d.parent
    if deal_info:
        if deal_info.get("lead"):
            return deal_info["lead"]
        if deal_info.get("team"):
            return deal_info["team"][0]
    partner = store._rows("SELECT email FROM users WHERE role = 'managing_partner' LIMIT 1")
    return partner[0]["email"] if partner else f"unknown@{DOMAIN}"


def infer_doc_kind(path: Path) -> str:
    name = path.name.lower()
    folders = [p.lower() for p in path.parent.parts[-3:]]
    rules = [
        ("ic memo", "ic_memo"), ("pass memo", "ic_memo"), ("deck", "deck"), ("model brief", "model_brief"),
        ("cim", "cim"), ("qoe", "diligence"), ("policy", "policy"), ("process", "process"),
        ("directory", "directory"), ("precedent", "precedent"), ("slack", "chat"),
    ]
    for needle, kind in rules:
        if needle in name:
            return kind
    if path.suffix.lower() == ".eml" or "email" in folders:
        return "email"
    if "calls" in folders or "transcript" in name or " call" in name:
        return "transcript"
    if "notes" in folders:
        return "chat"
    if "memos" in folders:
        return "memo"
    return "document"


# --------------------------------------------------------------------------- text extraction

def extract_text(path: Path) -> dict:
    """Return {text, title?, date?, author?, participants?, from?, to?, cc?} for a supported file."""
    ext = path.suffix.lower()
    if ext == ".eml":
        return _extract_eml(path)
    if ext == ".docx":
        from docx import Document

        d = Document(str(path))
        paras = [p.text for p in d.paragraphs if p.text.strip()]
        title = d.core_properties.title or (paras[0] if paras else None)
        if paras and paras[0] == title:
            paras = paras[1:]
        return {"text": "\n\n".join(paras), "title": title}
    if ext == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        text = "\n".join((page.extract_text() or "") for page in reader.pages).strip()
        title = (reader.metadata.title if reader.metadata else None) or None
        if title and text.startswith(title):
            text = text[len(title):].lstrip()
        return {"text": text, "title": title}
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    out: dict = {"text": text}
    lines = text.splitlines()
    if ext == ".md" and lines and lines[0].startswith("# "):
        out["title"] = lines[0][2:].strip()
        out["text"] = "\n".join(lines[1:]).strip()
    elif ext == ".txt" and len(lines) > 1 and lines[1].startswith("Date:"):
        out["title"] = lines[0].strip()
        header_end = next((i for i, line in enumerate(lines) if not line.strip()), len(lines))
        for line in lines[1:header_end]:
            key, _, value = line.partition(":")
            if key.strip() == "Date":
                out["date"] = value.strip()[:10]
            elif key.strip() == "Participants":
                out["participants"] = [p.strip() for p in value.split(",") if p.strip()]
        out["text"] = "\n".join(lines[header_end:]).strip()
    return out


def _extract_eml(path: Path) -> dict:
    msg = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)
    body = msg.get_body(preferencelist=("plain", "html"))
    text = body.get_content().strip() if body else ""
    addrs = lambda h: [a for _, a in getaddresses([msg.get(h, "")]) if a]  # noqa: E731
    sender = addrs("From")
    date = None
    if msg.get("Date"):
        try:
            date = parsedate_to_datetime(msg["Date"]).date().isoformat()
        except (TypeError, ValueError):
            date = None
    subject = str(msg.get("Subject", "")).strip()
    header = "\n".join(f"{h}: {msg[h]}" for h in ("From", "To", "Cc", "Date", "Subject") if msg.get(h))
    return {
        "text": f"{header}\n\n{text}", "title": f"Email: {subject}" if subject else None, "date": date,
        "author": sender[0] if sender else None, "from": sender[0] if sender else None,
        "to": addrs("To"), "cc": addrs("Cc"),
    }


# --------------------------------------------------------------------------- HydraDB

def _user_name(email_addr: str) -> str:
    row = store.get_user(email_addr)
    return row["name"] if row else email_addr


def app_item(doc: dict) -> dict:
    """HydraDB app_knowledge item. Field shape verified against the HydraDB v2 docs."""
    kind = doc["kind"]
    ts = f"{doc['date']}T12:00:00Z"
    author = _user_name(doc["author_email"])
    fields: dict = {"kind": kind, "body": doc["body"]}
    if kind == "email":
        fields.update({"subject": doc["title"], "from": doc.get("from") or doc["author_email"],
                       "to": doc.get("to", []), "cc": doc.get("cc", []), "created_at": ts})
    elif kind == "meeting":
        fields.update({"title": doc["title"], "host": author, "host_email": doc["author_email"],
                       "participants": [{"name": p} for p in doc.get("participants", [])], "started_at": ts})
    elif kind == "message":
        fields.update({"author": author, "created_at": ts})
    else:
        fields.update({"title": doc["title"], "created_by": author, "created_at": ts})
    return {
        "id": doc["doc_id"], "database": HYDRA_DATABASE, "collection": COLLECTION, "title": doc["title"],
        "kind": kind, "provider": "northbeam", "external_id": doc["doc_id"], "timestamp": ts, "fields": fields,
        "metadata": {"deal": doc["deal_id"] or "firm", "doc_kind": doc["doc_kind"]},
        "additional_metadata": {"author": doc["author_email"], "date": doc["date"]},
        "acl": doc["acl"],
    }


def hydra_ingest(items: list[dict]) -> None:
    client = hydra()
    for i in range(0, len(items), BATCH):
        client.context.ingest(type="knowledge", database=HYDRA_DATABASE, collection=COLLECTION, upsert="true",
                              app_knowledge=json.dumps(items[i:i + BATCH]))


def wait_until_searchable(ids: list[str], timeout: float = 600) -> None:
    client = hydra()
    pending, deadline = set(ids), time.time() + timeout
    while pending and time.time() < deadline:
        resp = client.context.status(database=HYDRA_DATABASE, collection=COLLECTION, ids=sorted(pending))
        for s in resp.data.statuses:
            if s.indexing_status in ("graph_creation", "completed"):
                pending.discard(s.id)
            elif s.indexing_status in ("errored", "failed"):
                raise RuntimeError(f"indexing failed for {s.id}: {s.error_code} {s.error_message or s.message}")
        if pending:
            time.sleep(3)
    if pending:
        raise TimeoutError(f"still indexing after {timeout}s: {sorted(pending)}")


def register_document(doc: dict) -> None:
    store.execute(
        "INSERT OR REPLACE INTO documents(doc_id, title, deal_id, kind, date, author_email, acl_json) "
        "VALUES (?,?,?,?,?,?,?)",
        (doc["doc_id"], doc["title"], doc["deal_id"], doc["doc_kind"], doc["date"], doc["author_email"],
         json.dumps(doc["acl"])),
    )


# --------------------------------------------------------------------------- quoted metrics

_QUOTE_SCHEMA = {
    "type": "object",
    "properties": {
        "quotes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"metric": {"type": "string", "enum": METRICS}, "value": {"type": "number"}},
                "required": ["metric", "value"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["quotes"],
    "additionalProperties": False,
}


def llm_extract_quotes(text: str) -> list[dict]:
    from openai import OpenAI

    response = OpenAI().responses.create(
        model=LLM_MODEL,
        reasoning={"effort": "low"},
        instructions=(
            "Extract the base-case financial model figures this document quotes. Metrics: revenue_growth "
            "(annual, as a fraction, 12% -> 0.12), ebitda_margin (fraction), fy26_revenue ($M), fy26_ebitda ($M), "
            "exit_multiple (x), irr (gross, fraction), moic (x). Only include metrics the text states explicitly "
            "for the base case; ignore downside or sensitivity cases and historical figures. At most one per metric."
        ),
        input=text[:12000],
        text={"format": {"type": "json_schema", "name": "quoted_metrics", "schema": _QUOTE_SCHEMA, "strict": True}},
    )
    return json.loads(response.output_text)["quotes"]


_NUM = r"(\d+(?:\.\d+)?)"
_REGEX = {
    "revenue_growth": [rf"{_NUM}% (?:annual )?(?:revenue )?growth", rf"growth[^.\n%]{{0,40}}?{_NUM}%"],
    "ebitda_margin": [rf"EBITDA margin[^.\n%]{{0,30}}?{_NUM}%", rf"{_NUM}% EBITDA margin"],
    "fy26_revenue": [rf"FY26 revenue[^$\n]{{0,20}}\${_NUM}M"],
    "fy26_ebitda": [rf"FY26 EBITDA[^$\n]{{0,20}}\${_NUM}M"],
    "exit_multiple": [rf"exit[^.\n]{{0,30}}?{_NUM}x", rf"{_NUM}x FY30 EBITDA"],
    "irr": [rf"IRR[^.\n%]{{0,20}}?{_NUM}%"],
    "moic": [rf"MOIC[^.\n]{{0,20}}?{_NUM}x", rf"{_NUM}x MOIC"],
}


def regex_extract_quotes(text: str) -> list[dict]:
    quotes = []
    for metric, patterns in _REGEX.items():
        for pattern in patterns:
            m = re.search(pattern, text, flags=re.IGNORECASE)
            if m:
                value = float(m.group(1))
                quotes.append({"metric": metric, "value": value / 100 if metric in PERCENT_METRICS else value})
                break
    return quotes


def extract_quotes(text: str) -> list[dict]:
    try:
        quotes = llm_extract_quotes(text)
    except Exception:
        quotes = []
    if not quotes:
        quotes = regex_extract_quotes(text)
    seen, out = set(), []
    for q in quotes:
        if q["metric"] in METRICS and q["metric"] not in seen:
            seen.add(q["metric"])
            out.append(q)
    return out


def record_references(doc: dict, text: str) -> int:
    """Store the model numbers a memo or deck quotes, pinned to the commit they came from."""
    from app import vcs

    quotes = extract_quotes(text)
    rows = vcs.add_doc_references(doc["doc_id"], doc["title"], doc["deal_id"], doc["author_email"],
                                  {q["metric"]: q["value"] for q in quotes}, doc_date=f"{doc['date']}T23:59:59")
    return len(rows)


# --------------------------------------------------------------------------- commit documents

def _model_of(commit: dict) -> tuple[str, str]:
    model = commit.get("model")
    model_id = commit.get("model_id") or (model.get("id") if isinstance(model, dict) else model)
    name = model.get("name") if isinstance(model, dict) else None
    if not name:
        row = store._rows("SELECT name FROM models WHERE id = ?", (model_id,))
        name = row[0]["name"] if row else str(model_id)
    return model_id, name


def commit_doc(commit: dict, deal_id: str) -> dict:
    """Change-log document for one commit: what changed, never why (the why lives in other documents)."""
    model_id, model_name = _model_of(commit)
    short = commit["id"][:7]
    when = commit["committed_at"]
    lines = [
        f"{model_name} - commit {short} on branch {commit.get('branch')}",
        f"Author: {_user_name(commit['author_email'])} ({commit['author_email']}). Saved: {when}.",
        f"Message: {commit.get('message') or ''}",
    ]
    if commit.get("parent_id"):
        lines.append(f"Parent commit: {commit['parent_id'][:7]}"
                     + (" (forked into a new branch)" if commit.get("is_fork") else ""))
    else:
        lines.append("First commit of this model.")
    lines.append(f"File: {Path(commit.get('file_path') or '').name}")
    prior, metrics = commit.get("prior_metrics") or {}, commit.get("metrics") or {}
    if metrics:
        lines.append("\nKey metrics after this commit:")
        for m in METRICS:
            if m in metrics:
                before = f" (was {_fmt_metric(m, prior[m])})" if m in prior and prior[m] != metrics[m] else ""
                lines.append(f"- {METRIC_LABELS[m]}: {_fmt_metric(m, metrics[m])}{before}")
    changes = commit.get("changes") or []
    if changes:
        lines.append(f"\nCell changes ({len(changes)}):")
        for ch in changes[:40]:
            lines.append(f"- {ch['sheet']}!{ch['cell']} {ch.get('label') or ''}: {ch.get('old_value')} -> "
                         f"{ch.get('new_value')}")
    return {
        "doc_id": f"f_commit_{commit['id']}", "title": f"{model_name} - commit {short} ({commit.get('branch')})",
        "deal_id": deal_id, "kind": "knowledge_base", "doc_kind": "changelog", "date": when[:10],
        "author_email": commit["author_email"], "acl": deal_acl(deal_id), "body": "\n".join(lines),
    }


def model_summary_doc(commit: dict, deal_id: str, path: Path) -> dict | None:
    model_id, model_name = _model_of(commit)
    doc_id = f"f_model_{model_id}"
    if store.get_document(doc_id):
        return None
    from app import workbook

    wb = workbook.read_workbook(path)
    metrics = commit.get("metrics") or {}
    body = [f"{model_name}: financial model for deal {deal_id}. Sheets: {', '.join(wb['order'])}.",
            f"First seen in file {path.name}, saved {commit['committed_at']} by {_user_name(commit['author_email'])}."]
    if metrics:
        body.append("Key metrics at first commit: " + "; ".join(
            f"{METRIC_LABELS[m]} {_fmt_metric(m, metrics[m])}" for m in METRICS if m in metrics) + ".")
    body.append("\nLabelled cells:\n" + workbook.dump_for_llm(wb, max_cells=150))
    return {
        "doc_id": doc_id, "title": f"{model_name} - model overview", "deal_id": deal_id,
        "kind": "knowledge_base", "doc_kind": "model_summary", "date": commit["committed_at"][:10],
        "author_email": commit["author_email"], "acl": deal_acl(deal_id), "body": "\n".join(body),
    }


# --------------------------------------------------------------------------- scan

def _seen(rel: str) -> dict | None:
    rows = store._rows("SELECT * FROM files_seen WHERE path = ?", (rel,))
    return rows[0] if rows else None


def _mark_seen(rel: str, digest: str, mtime: float, kind: str, doc_id: str | None = None,
               commit_id: str | None = None) -> None:
    store.execute(
        "INSERT OR REPLACE INTO files_seen(path, sha256, mtime, kind, doc_id, commit_id) VALUES (?,?,?,?,?,?)",
        (rel, digest, mtime, kind, doc_id, commit_id),
    )


@contextmanager
def exclusive():
    """Serialize imports across threads and processes (the server's watcher and scripts/seed.py)."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _scan_lock, open(config.DATA_DIR / ".ingest.lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def scan(root: Path | str = IMPORT_ROOT, wait: bool = False, lock: bool = True) -> dict:
    """Import new or changed files under root. Returns {commits, docs, changelogs, skipped, errors}.

    Pass lock=False only when the caller already holds `exclusive()`.
    """
    root = Path(root).resolve()
    if not lock:
        return _scan(root, wait)
    with exclusive():
        return _scan(root, wait)


def _scan(root: Path, wait: bool) -> dict:
    from app import vcs

    result: dict = {"commits": [], "docs": [], "changelogs": [], "skipped": 0, "errors": []}
    deal_cache: dict[str, dict] = {}

    def deal_info(deal_id: str | None, deal_dir: Path | None) -> dict | None:
        if deal_id is None:
            return None
        if deal_id not in deal_cache:
            deal_cache[deal_id] = ensure_deal(deal_id, deal_dir)
        return deal_cache[deal_id]

    excel, texts = [], []
    for path in root.rglob("*"):
        if not path.is_file() or is_ignored(path) or any(p.startswith(".") for p in path.relative_to(root).parts):
            continue
        ext = path.suffix.lower()
        if ext in EXCEL_EXT:
            excel.append(path)
        elif ext in TEXT_EXT:
            texts.append(path)

    for deal_dir in [p for p in root.iterdir() if p.is_dir() and not p.name.startswith((".", "_"))]:
        deal_info(slug(deal_dir.name), deal_dir)

    pending: list[tuple[dict, str, str, float, str, str | None]] = []  # (doc, rel, digest, mtime, kind, commit)
    quoting: list[tuple[dict, str]] = []

    # Excel first, oldest save first, so parents exist before children and documents can pin commits.
    for path in sorted(excel, key=lambda p: p.stat().st_mtime):
        rel = str(path.relative_to(root))
        mtime = path.stat().st_mtime
        digest = sha256(path)
        seen = _seen(rel)
        if seen and seen["sha256"] == digest:
            result["skipped"] += 1
            continue
        deal_id, deal_dir = resolve_deal(path, root)
        if deal_id is None:
            result["skipped"] += 1
            continue
        info = deal_info(deal_id, deal_dir)
        author = resolve_author(path, deal_dir, info)
        try:
            commit = vcs.record_snapshot(path, deal_id, author, iso_from_mtime(mtime))
        except Exception as e:
            result["errors"].append({"path": rel, "error": f"{type(e).__name__}: {e}"})
            continue
        if commit is None:
            _mark_seen(rel, digest, mtime, "excel")
            result["skipped"] += 1
            continue
        commit = {**commit, "deal_id": deal_id}
        result["commits"].append(commit)
        log = commit_doc(commit, deal_id)
        pending.append((log, rel, digest, mtime, "excel", commit["id"]))
        summary = model_summary_doc(commit, deal_id, path)
        if summary:
            pending.append((summary, "", "", 0, "", None))

    for path in sorted(texts, key=lambda p: p.stat().st_mtime):
        rel = str(path.relative_to(root))
        mtime = path.stat().st_mtime
        digest = sha256(path)
        seen = _seen(rel)
        if seen and seen["sha256"] == digest:
            result["skipped"] += 1
            continue
        deal_id, deal_dir = resolve_deal(path, root)
        info = deal_info(deal_id, deal_dir)
        try:
            meta = extract_text(path)
        except Exception as e:
            result["errors"].append({"path": rel, "error": f"{type(e).__name__}: {e}"})
            continue
        if not meta.get("text"):
            result["skipped"] += 1
            continue
        doc_kind = infer_doc_kind(path)
        author = meta.get("author") if store.get_user(meta.get("author") or "") else None
        doc = {
            "doc_id": doc_id_for(rel),
            "title": meta.get("title") or path.stem,
            "deal_id": deal_id,
            "kind": HYDRA_KIND.get(doc_kind, "knowledge_base"),
            "doc_kind": doc_kind,
            "date": meta.get("date") or iso_from_mtime(mtime)[:10],
            "author_email": author or resolve_author(path, deal_dir, info),
            "acl": deal_acl(deal_id),
            "body": meta["text"],
            "participants": meta.get("participants", []),
            "from": meta.get("from"), "to": meta.get("to", []), "cc": meta.get("cc", []),
            "source_path": rel,
        }
        pending.append((doc, rel, digest, mtime, "doc", None))
        if deal_id and doc_kind in QUOTING_KINDS and store._rows("SELECT 1 FROM models WHERE deal_id = ?", (deal_id,)):
            quoting.append((doc, meta["text"]))

    if pending:
        try:
            hydra_ingest([app_item(d) for d, *_ in pending])
        except Exception as e:
            result["errors"].append({"path": "*", "error": f"HydraDB ingest failed: {type(e).__name__}: {e}"})
            return result
    for doc, rel, digest, mtime, kind, commit_id in pending:
        register_document(doc)
        if rel:
            _mark_seen(rel, digest, mtime, kind, doc_id=doc["doc_id"], commit_id=commit_id)
        entry = {"doc_id": doc["doc_id"], "title": doc["title"], "deal_id": doc["deal_id"],
                 "doc_kind": doc["doc_kind"], "path": rel}
        (result["changelogs"] if doc["doc_kind"] in ("changelog", "model_summary") else result["docs"]).append(entry)

    for doc, text in quoting:
        try:
            record_references(doc, text)
        except Exception as e:
            result["errors"].append({"path": doc["source_path"], "error": f"quote extraction: {e}"})

    if wait and pending:
        wait_until_searchable([d["doc_id"] for d, *_ in pending])
    return result
