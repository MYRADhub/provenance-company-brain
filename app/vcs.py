"""Git-style history for financial models.

Every observed state of a workbook is a commit (id = content hash). Files are grouped into models by
structure; each file path is a branch; a new file in an existing model forks from the most similar
earlier commit. Snapshots are stored as JSON under data/snapshots so any two commits can be diffed
even after the files on disk have changed.
"""

import datetime as dt
import json
import os
import re
from pathlib import Path

from app import config, store, workbook

MODEL_MATCH_THRESHOLD = 0.5
METRICS = ["revenue_growth", "ebitda_margin", "fy26_revenue", "fy26_ebitda", "exit_multiple", "irr", "moic"]
METRIC_DESCRIPTIONS = {
    "revenue_growth": "annual revenue growth rate assumption",
    "ebitda_margin": "EBITDA margin assumption",
    "fy26_revenue": "FY26 revenue",
    "fy26_ebitda": "FY26 EBITDA",
    "exit_multiple": "exit EV/EBITDA multiple",
    "irr": "equity IRR",
    "moic": "equity MOIC / multiple of money",
}
VERSION_TOKENS = re.compile(
    r"\b(v\d+(\.\d+)*|ver\s*\d+|version\s*\d+|final|draft|copy|latest|clean|updated|rev\s*\d+)\b"
    r"|\(\d+\)|\b\d{4}[-_.]\d{2}[-_.]\d{2}\b|\b\d{6,8}\b",
    re.IGNORECASE,
)


# ----------------------------------------------------------------- helpers

def _snapshot_dir() -> Path:
    path = config.DATA_DIR / "snapshots"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _save_snapshot(commit_id: str, wb: dict) -> None:
    (_snapshot_dir() / f"{commit_id}.json").write_text(json.dumps(wb, default=str))


def load_snapshot(commit_id: str) -> dict:
    return json.loads((_snapshot_dir() / f"{commit_id}.json").read_text())


def _iso(value) -> str:
    if value is None:
        return dt.datetime.now().replace(microsecond=0).isoformat()
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value).replace(microsecond=0).isoformat()
    if isinstance(value, dt.datetime):
        return value.replace(microsecond=0).isoformat()
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time()).isoformat()
    return str(value)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _clean_stem(stem: str) -> str:
    """Remove version noise from a file stem: 'Falcon LBO v3 FINAL (2)' -> 'Falcon LBO'."""
    s = VERSION_TOKENS.sub(" ", stem)
    s = re.sub(r"[\s_\-]+", " ", s)
    s = re.sub(r"\(\s*\)", " ", s)
    return re.sub(r"\s+", " ", s).strip(" -_")


def _branch_suffix(stem: str, model_name: str) -> str:
    """What distinguishes this file from the model: 'Falcon LBO v2 - downside case (PS)' -> 'downside-case-ps'."""
    rest = stem
    if rest.lower().startswith(model_name.lower()):
        rest = rest[len(model_name):]
    else:
        tokens = [t for t in re.split(r"\s+", model_name) if t]
        for t in tokens:
            rest = re.sub(rf"\b{re.escape(t)}\b", " ", rest, flags=re.IGNORECASE)
    return _slug(_clean_stem(rest))


def _encode(value) -> str | None:
    return None if value is None else json.dumps(value, default=str)


def _decode(value):
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _openai():
    if not os.environ.get("OPENAI_API_KEY"):
        return None
    from openai import OpenAI

    return OpenAI()


# ----------------------------------------------------------------- LLM steps (each has a fallback)

def _heuristic_metric_cells(wb: dict) -> dict[str, tuple[str, str]]:
    rules = {
        "revenue_growth": lambda l: "growth" in l and "revenue" in l,
        "ebitda_margin": lambda l: "ebitda" in l and "margin" in l,
        "fy26_revenue": lambda l: "fy26" in l and "revenue" in l and "growth" not in l,
        "fy26_ebitda": lambda l: "fy26" in l and "ebitda" in l and "margin" not in l,
        "exit_multiple": lambda l: "exit" in l and "multiple" in l,
        "irr": lambda l: re.search(r"\birr\b", l) is not None,
        "moic": lambda l: "moic" in l or "multiple of money" in l,
    }
    found: dict[str, tuple[str, str]] = {}
    for sheet in wb["order"]:
        for ref, cell in wb["sheets"][sheet].items():
            v = cell["value"]
            if not isinstance(v, (int, float)) or isinstance(v, bool):
                continue
            label = (cell["label"] or "").lower()
            for metric, rule in rules.items():
                if metric not in found and rule(label):
                    found[metric] = (sheet, ref)
    return found


def llm_metric_cells(wb: dict) -> dict[str, tuple[str, str]]:
    """Ask the LLM which cells hold the key metrics. Returns {} on any failure."""
    client = _openai()
    if client is None:
        return {}
    schema = {
        "type": "object",
        "properties": {
            "metrics": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "metric": {"type": "string", "enum": METRICS},
                        "cell": {"type": "string", "description": "Sheet!A1 reference"},
                    },
                    "required": ["metric", "cell"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["metrics"],
        "additionalProperties": False,
    }
    wanted = "\n".join(f"- {m}: {d}" for m, d in METRIC_DESCRIPTIONS.items())
    prompt = (
        "Below are the labelled numeric cells of a private equity financial model "
        "(format: Sheet!Cell | label | cached value).\n"
        f"Find the single cell that holds each of these key metrics, only if present:\n{wanted}\n"
        "Prefer the headline input or output cell (not a per-year intermediate unless the metric is year-specific).\n\n"
        + workbook.dump_for_llm(wb)
    )
    try:
        resp = client.responses.create(
            model=config.LLM_MODEL,
            reasoning={"effort": "low"},
            input=prompt,
            text={"format": {"type": "json_schema", "name": "metric_cells", "schema": schema, "strict": True}},
        )
        items = json.loads(resp.output_text)["metrics"]
    except Exception:
        return {}
    out: dict[str, tuple[str, str]] = {}
    for item in items:
        ref = item.get("cell", "")
        if "!" not in ref:
            continue
        sheet, cell = ref.rsplit("!", 1)
        sheet = sheet.strip("'")
        target = wb["sheets"].get(sheet, {}).get(cell.replace("$", ""))
        if target and isinstance(target["value"], (int, float)) and not isinstance(target["value"], bool):
            out.setdefault(item["metric"], (sheet, cell.replace("$", "")))
    return out


def _fmt_value(v) -> str:
    if isinstance(v, float):
        return f"{v:.4g}"
    return "empty" if v is None else str(v)


def _fallback_message(changes: list[dict], parent_id: str | None, is_fork: bool, branch: str) -> str:
    if parent_id is None:
        return "Initial import"
    labelled = [c for c in changes if c["label"]] or changes
    if not labelled:
        return f"Branch {branch} from {parent_id[:7]}" if is_fork else "No cell changes"
    parts = [f"{c['label']} {_fmt_value(c['old_value'])} -> {_fmt_value(c['new_value'])}" for c in labelled[:3]]
    more = len(changes) - min(3, len(labelled))
    msg = "; ".join(parts) + (f" (+{more} cells)" if more > 0 else "")
    return (f"Branch {branch}: " if is_fork else "") + msg


def llm_commit_message(changes: list[dict], metrics: dict, prior_metrics: dict, branch: str, is_fork: bool) -> str | None:
    """One-line git-style commit message from a cell diff. None on any failure."""
    client = _openai()
    if client is None or not changes:
        return None
    lines = [
        f"{c['sheet']}!{c['cell']} [{c['label']}] {_fmt_value(c['old_value'])} -> {_fmt_value(c['new_value'])}"
        + (f" (formula {c['old_formula']} -> {c['new_formula']})" if c["old_formula"] != c["new_formula"] else "")
        for c in changes[:60]
    ]
    metric_lines = [
        f"{m}: {_fmt_value(prior_metrics.get(m))} -> {_fmt_value(metrics.get(m))}"
        for m in METRICS if m in metrics or m in prior_metrics
    ]
    prompt = (
        "Write a one-line git commit message (max 100 characters, imperative mood, no trailing period) "
        "summarising this change to a private equity financial model. Lead with the input assumptions that "
        "changed, then the headline output impact with old -> new values. Do not speculate about why.\n"
        + (f"This file is a new branch '{branch}' forked from another version.\n" if is_fork else "")
        + f"Changed cells ({len(changes)} total):\n" + "\n".join(lines)
        + ("\nKey metrics:\n" + "\n".join(metric_lines) if metric_lines else "")
    )
    try:
        resp = client.responses.create(model=config.LLM_MODEL, reasoning={"effort": "low"}, input=prompt)
        msg = (resp.output_text or "").strip().splitlines()[0].strip().strip('"')
        return msg[:140] or None
    except Exception:
        return None


# ----------------------------------------------------------------- metrics

def _metric_cells(model_id: str, wb: dict) -> dict[str, tuple[str, str]]:
    rows = store._rows("SELECT metric, sheet, cell FROM model_metric_cells WHERE model_id = ?", (model_id,))
    if rows:
        return {r["metric"]: (r["sheet"], r["cell"]) for r in rows}
    mapping = llm_metric_cells(wb) or {}
    for metric, loc in _heuristic_metric_cells(wb).items():
        mapping.setdefault(metric, loc)
    if mapping:
        store.executemany(
            "INSERT OR REPLACE INTO model_metric_cells(model_id, metric, sheet, cell) VALUES (?,?,?,?)",
            [(model_id, m, s, c) for m, (s, c) in mapping.items()],
        )
    return mapping


def _extract_metrics(wb: dict, cells: dict[str, tuple[str, str]]) -> dict:
    out = {}
    for metric, (sheet, ref) in cells.items():
        v = wb["sheets"].get(sheet, {}).get(ref, {}).get("value")
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[metric] = float(v)
    return out


def commit_metrics(commit_id: str | None) -> dict:
    if not commit_id:
        return {}
    rows = store._rows("SELECT metric, value FROM commit_metrics WHERE commit_id = ?", (commit_id,))
    return {r["metric"]: r["value"] for r in rows}


# ----------------------------------------------------------------- recording

def _commit_row(commit_id: str) -> dict | None:
    rows = store._rows("SELECT * FROM commits WHERE id = ?", (commit_id,))
    return rows[0] if rows else None


def _resolve_model(deal_id: str, wb: dict, stem: str, committed_at: str) -> tuple[dict, bool]:
    """Find the model this workbook belongs to, or create one. Returns (model, created)."""
    best, best_score = None, 0.0
    for model in store._rows("SELECT * FROM models WHERE deal_id = ? ORDER BY created_at", (deal_id,)):
        heads = store._rows(
            "SELECT id FROM commits WHERE model_id = ? ORDER BY committed_at DESC LIMIT 5", (model["id"],)
        )
        for h in heads:
            score = workbook.structure_similarity(load_snapshot(h["id"]), wb)
            if score > best_score:
                best, best_score = model, score
    if best is not None and best_score >= MODEL_MATCH_THRESHOLD:
        return best, False
    name = _clean_stem(stem) or stem
    model_id = f"{_slug(deal_id)}-{_slug(name)}"[:60]
    suffix, base = 2, model_id
    while store._rows("SELECT 1 FROM models WHERE id = ?", (model_id,)):
        model_id = f"{base}-{suffix}"
        suffix += 1
    model = {"id": model_id, "deal_id": deal_id, "name": name, "created_at": committed_at}
    store.execute("INSERT INTO models(id, deal_id, name, created_at) VALUES (?,?,?,?)",
                  (model_id, deal_id, name, committed_at))
    return model, True


def _unique_branch(model_id: str, name: str) -> str:
    name = name or "branch"
    candidate, n = name, 2
    while store._rows("SELECT 1 FROM branches WHERE model_id = ? AND name = ?", (model_id, candidate)):
        candidate = f"{name}-{n}"
        n += 1
    return candidate


def _set_branch(model_id: str, name: str, head: str, file_path: str) -> None:
    store.execute(
        "INSERT INTO branches(model_id, name, head_commit, file_path) VALUES (?,?,?,?) "
        "ON CONFLICT(model_id, name) DO UPDATE SET head_commit = excluded.head_commit, file_path = excluded.file_path",
        (model_id, name, head, file_path),
    )


def _branch_for_path(path: str) -> dict | None:
    rows = store._rows("SELECT * FROM branches WHERE file_path = ?", (path,))
    if rows:
        return rows[0]
    rows = store._rows(
        "SELECT c.model_id, c.branch AS name, c.id AS head_commit, c.file_path FROM commits c "
        "WHERE c.file_path = ? ORDER BY c.committed_at DESC LIMIT 1",
        (path,),
    )
    return rows[0] if rows else None


def record_snapshot(path: str | Path, deal_id: str, author_email: str, saved_at=None) -> dict | None:
    """Record the current state of an Excel file as a commit.

    Returns None when the content is already known (unchanged re-save or an unedited copy);
    otherwise the new commit with its cell changes and key metrics.
    """
    path = str(Path(path).resolve())
    wb = workbook.read_workbook(path)
    commit_id = workbook.cell_map_hash(wb)[:10]
    committed_at = _iso(saved_at if saved_at is not None else os.path.getmtime(path))
    mtime = os.path.getmtime(path)
    stem = Path(path).stem

    existing = _commit_row(commit_id)
    known_branch = _branch_for_path(path)
    if existing:
        if known_branch is None:
            # An unedited copy: register the file as a branch pointing at the existing commit.
            model = store._rows("SELECT * FROM models WHERE id = ?", (existing["model_id"],))[0]
            name = _unique_branch(model["id"], _branch_suffix(stem, model["name"]) or "copy")
            _set_branch(model["id"], name, commit_id, path)
        return None

    is_fork = False
    if known_branch is not None:
        model = store._rows("SELECT * FROM models WHERE id = ?", (known_branch["model_id"],))[0]
        branch = known_branch["name"]
        parent_id = known_branch["head_commit"]
        current_head = store._rows(
            "SELECT head_commit FROM branches WHERE model_id = ? AND name = ?", (model["id"], branch)
        )
        if current_head and current_head[0]["head_commit"] != parent_id:
            # The branch moved on to another file; a re-save of an old file is a fork from its last state.
            is_fork = True
            branch = _unique_branch(model["id"], _branch_suffix(stem, model["name"]) or _slug(stem))
    else:
        model, created = _resolve_model(deal_id, wb, stem, committed_at)
        if created:
            branch, parent_id = "main", None
        else:
            parent_id = _most_similar_commit(model["id"], wb, committed_at)
            suffix = _branch_suffix(stem, model["name"])
            head_of = store._rows(
                "SELECT name FROM branches WHERE model_id = ? AND head_commit = ?", (model["id"], parent_id)
            )
            if not suffix and head_of:
                # 'Falcon LBO v3.xlsx' after 'Falcon LBO v2.xlsx': the next version on the same line.
                branch = head_of[0]["name"]
            else:
                is_fork = True
                version = VERSION_TOKENS.search(stem)
                branch = _unique_branch(model["id"], suffix or _slug(version.group(0) if version else stem))

    parent_wb = load_snapshot(parent_id) if parent_id else None
    changes = workbook.diff(parent_wb, wb) if parent_wb else []
    cells = _metric_cells(model["id"], wb)
    metrics = _extract_metrics(wb, cells)
    prior_metrics = commit_metrics(parent_id)
    message = (
        llm_commit_message(changes, metrics, prior_metrics, branch, is_fork) if parent_id else None
    ) or _fallback_message(changes, parent_id, is_fork, branch)

    _save_snapshot(commit_id, wb)
    store.execute(
        "INSERT INTO commits(id, model_id, branch, parent_id, author_email, committed_at, message, file_path, file_mtime) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (commit_id, model["id"], branch, parent_id, author_email, committed_at, message, path, mtime),
    )
    store.executemany(
        "INSERT INTO cell_changes(commit_id, sheet, cell, label, old_value, new_value, old_formula, new_formula) "
        "VALUES (?,?,?,?,?,?,?,?)",
        [(commit_id, c["sheet"], c["cell"], c["label"], _encode(c["old_value"]), _encode(c["new_value"]),
          c["old_formula"], c["new_formula"]) for c in changes],
    )
    store.executemany(
        "INSERT OR REPLACE INTO commit_metrics(commit_id, metric, value) VALUES (?,?,?)",
        [(commit_id, m, v) for m, v in metrics.items()],
    )
    _set_branch(model["id"], branch, commit_id, path)

    return {
        "id": commit_id,
        "model_id": model["id"],
        "model": {"id": model["id"], "name": model["name"], "deal_id": model["deal_id"]},
        "deal_id": model["deal_id"],
        "branch": branch,
        "parent_id": parent_id,
        "is_fork": is_fork,
        "author_email": author_email,
        "committed_at": committed_at,
        "message": message,
        "file_path": path,
        "changes": changes,
        "metrics": metrics,
        "prior_metrics": prior_metrics,
    }


def _most_similar_commit(model_id: str, wb: dict, before: str) -> str | None:
    rows = store._rows(
        "SELECT id, committed_at FROM commits WHERE model_id = ? ORDER BY committed_at", (model_id,)
    )
    candidates = [r for r in rows if r["committed_at"] <= before] or rows
    best, best_score = None, -1.0
    for r in candidates:
        score = workbook.similarity(load_snapshot(r["id"]), wb)
        # Ties go to the later commit: the most recent state someone could have copied.
        if score >= best_score:
            best, best_score = r["id"], score
    return best


# ----------------------------------------------------------------- reading

def get_commit(commit_id: str) -> dict | None:
    row = _commit_row(commit_id)
    if not row:
        return None
    changes = store._rows("SELECT * FROM cell_changes WHERE commit_id = ? ORDER BY rowid", (commit_id,))
    for c in changes:
        c.pop("commit_id", None)
        c["old_value"], c["new_value"] = _decode(c["old_value"]), _decode(c["new_value"])
    return {**row, "metrics": commit_metrics(commit_id), "prior_metrics": commit_metrics(row["parent_id"]),
            "changes": changes}


def commit_diff(a: str, b: str) -> list[dict]:
    """Cell-level changes from commit a to commit b (any two commits, not only parent and child)."""
    if a == b:
        return []
    return workbook.diff(load_snapshot(a), load_snapshot(b))


def list_models(deal_id: str) -> list[dict]:
    models = store._rows("SELECT * FROM models WHERE deal_id = ? ORDER BY created_at", (deal_id,))
    for m in models:
        branches = store._rows("SELECT name FROM branches WHERE model_id = ? ORDER BY rowid", (m["id"],))
        m["branches"] = [b["name"] for b in branches]
        m["commit_count"] = store._rows("SELECT COUNT(*) AS n FROM commits WHERE model_id = ?", (m["id"],))[0]["n"]
        m["head"] = main_head(m["id"])
    return models


def main_head(model_id: str) -> str | None:
    rows = store._rows("SELECT head_commit FROM branches WHERE model_id = ? AND name = 'main'", (model_id,))
    if rows:
        return rows[0]["head_commit"]
    rows = store._rows(
        "SELECT id FROM commits WHERE model_id = ? ORDER BY committed_at DESC LIMIT 1", (model_id,)
    )
    return rows[0]["id"] if rows else None


def latest_metrics(model_id: str) -> dict:
    return commit_metrics(main_head(model_id))


def graph(model_id: str) -> dict:
    """Everything a git-graph view needs: commits in time order with lanes, branches and tags."""
    models = store._rows("SELECT * FROM models WHERE id = ?", (model_id,))
    if not models:
        return {"model": None, "commits": [], "branches": [], "tags": []}
    commits = store._rows(
        "SELECT id, parent_id, branch, author_email, committed_at, message, file_path FROM commits "
        "WHERE model_id = ? ORDER BY committed_at, rowid",
        (model_id,),
    )
    lanes: dict[str, int] = {}
    for c in commits:
        lanes.setdefault(c["branch"], len(lanes))
    branches = store._rows(
        "SELECT name, head_commit, file_path FROM branches WHERE model_id = ? ORDER BY rowid", (model_id,)
    )
    for b in branches:
        lanes.setdefault(b["name"], len(lanes))
        b["lane"] = lanes[b["name"]]
    branches.sort(key=lambda b: b["lane"])

    tag_rows = store._rows(
        "SELECT DISTINCT quoted_commit AS commit_id, doc_id, doc_title FROM doc_references "
        "WHERE model_id = ? AND quoted_commit IS NOT NULL ORDER BY doc_title",
        (model_id,),
    )
    tags_by_commit: dict[str, list[dict]] = {}
    for t in tag_rows:
        tags_by_commit.setdefault(t["commit_id"], []).append({"doc_id": t["doc_id"], "doc_title": t["doc_title"]})
    heads = {b["head_commit"]: b["name"] for b in branches}
    for c in commits:
        c["lane"] = lanes[c["branch"]]
        c["metrics"] = commit_metrics(c["id"])
        c["tags"] = tags_by_commit.get(c["id"], [])
        c["head_of"] = [b["name"] for b in branches if b["head_commit"] == c["id"]]
        c["is_head"] = c["id"] in heads
    return {"model": models[0], "commits": commits, "branches": branches, "tags": tag_rows}


# ----------------------------------------------------------------- documents that quote numbers

def match_commit(model_id: str, quoted: dict[str, float], before: str | None = None) -> str | None:
    """The commit a document was based on: best metric match, latest first, not after the document date."""
    rows = store._rows(
        "SELECT id, committed_at FROM commits WHERE model_id = ? ORDER BY committed_at DESC", (model_id,)
    )
    if before:
        rows = [r for r in rows if r["committed_at"] <= before] or rows
    best, best_score = None, -1
    for r in rows:
        metrics = commit_metrics(r["id"])
        score = sum(1 for m, v in quoted.items() if m in metrics and abs(metrics[m] - v) <= max(1e-6, abs(v) * 0.005))
        if score > best_score:
            best, best_score = r["id"], score
    return best if best_score > 0 else None


def add_doc_references(doc_id: str, doc_title: str, deal_id: str, owner_email: str,
                       quoted: dict[str, float], doc_date: str | None = None) -> list[dict]:
    """Record which model numbers a document quotes, pinned to the commit they came from."""
    quoted = {m: float(v) for m, v in quoted.items() if m in METRICS and v is not None}
    if not quoted:
        return []
    best_model, best_commit, best_score = None, None, -1
    for model in store._rows("SELECT id FROM models WHERE deal_id = ?", (deal_id,)):
        commit_id = match_commit(model["id"], quoted, doc_date)
        metrics = commit_metrics(commit_id)
        score = sum(1 for m, v in quoted.items() if m in metrics and abs(metrics[m] - v) <= max(1e-6, abs(v) * 0.005))
        if score > best_score:
            best_model, best_commit, best_score = model["id"], commit_id, score
    store.execute("DELETE FROM doc_references WHERE doc_id = ?", (doc_id,))
    rows = [(doc_id, doc_title, deal_id, best_model, m, v, best_commit, owner_email) for m, v in quoted.items()]
    store.executemany(
        "INSERT INTO doc_references(doc_id, doc_title, deal_id, model_id, metric, quoted_value, quoted_commit, owner_email) "
        "VALUES (?,?,?,?,?,?,?,?)",
        rows,
    )
    return [dict(zip(["doc_id", "doc_title", "deal_id", "model_id", "metric", "quoted_value", "quoted_commit",
                      "owner_email"], r)) for r in rows]


def stale_references(deal_id: str) -> list[dict]:
    """Quoted numbers that no longer match the head of the model's main branch."""
    refs = store._rows("SELECT * FROM doc_references WHERE deal_id = ? ORDER BY doc_id, rowid", (deal_id,))
    stale = []
    latest_cache: dict[str, tuple[str | None, dict]] = {}
    for r in refs:
        if not r["model_id"]:
            continue
        if r["model_id"] not in latest_cache:
            head = main_head(r["model_id"])
            latest_cache[r["model_id"]] = (head, commit_metrics(head))
        head, metrics = latest_cache[r["model_id"]]
        current = metrics.get(r["metric"])
        if current is None or abs(current - r["quoted_value"]) <= max(1e-6, abs(r["quoted_value"]) * 0.005):
            continue
        stale.append({**r, "latest_value": current, "latest_commit": head})
    return stale
