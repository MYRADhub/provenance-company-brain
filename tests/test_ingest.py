import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

import pytest

from app import ingest, store, vcs, watcher

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import make_samples  # noqa: E402


@pytest.fixture
def offline(monkeypatch):
    """Capture HydraDB items instead of sending them; force the regex path for quote extraction."""
    sent: list[dict] = []
    monkeypatch.setattr(ingest, "hydra_ingest", lambda items: sent.extend(items))

    def no_llm(text):
        raise RuntimeError("offline")

    monkeypatch.setattr(ingest, "llm_extract_quotes", no_llm)
    return sent


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    """Build the sample corpus once into a temp dir."""
    root = tmp_path_factory.mktemp("samples")
    orig = (make_samples.SAMPLES, make_samples.DEALS_ROOT, make_samples.LIVE)
    make_samples.SAMPLES = root
    make_samples.DEALS_ROOT = root / "Northbeam Deals"
    make_samples.LIVE = root / "_live"
    try:
        make_samples.build()
    finally:
        make_samples.SAMPLES, make_samples.DEALS_ROOT, make_samples.LIVE = orig
    return root


@pytest.fixture
def deals_root(corpus, tmp_path):
    dst = tmp_path / "Northbeam Deals"
    shutil.copytree(corpus / "Northbeam Deals", dst)
    for src in (corpus / "Northbeam Deals").rglob("*"):  # copytree keeps mtimes, but be explicit
        if src.is_file():
            st = src.stat()
            os.utime(dst / src.relative_to(corpus / "Northbeam Deals"), (st.st_atime, st.st_mtime))
    return dst


def touch(path: Path, text: str = "x", when: str = "2026-10-01T12:00:00") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    ts = datetime.fromisoformat(when).timestamp()
    os.utime(path, (ts, ts))
    return path


# --------------------------------------------------------------------------- deal resolution

def test_resolve_deal_from_first_folder(tmp_path):
    root = tmp_path
    assert ingest.resolve_deal(root / "Falcon" / "Memos" / "a.docx", root) == ("falcon", root / "Falcon")
    assert ingest.resolve_deal(root / "Project Blue Sky" / "x.txt", root) == ("project-blue-sky", root / "Project Blue Sky")
    assert ingest.resolve_deal(root / "_firm" / "Policies" / "p.docx", root)[0] is None
    assert ingest.resolve_deal(root / "loose.txt", root) == (None, None)


def test_ensure_deal_reads_deal_json_and_replaces_team(tmp_path):
    d = tmp_path / "Osprey"
    d.mkdir()
    (d / "deal.json").write_text(json.dumps({"codename": "Project Osprey", "company": "Osprey Inc",
                                             "lead": "david@northbeam.com",
                                             "team": ["david@northbeam.com", "marcus@northbeam.com"]}))
    info = ingest.ensure_deal("osprey", d)
    assert info["codename"] == "Project Osprey"
    assert store.can_access_deal("marcus@northbeam.com", "osprey")
    assert not store.can_access_deal("priya@northbeam.com", "osprey")


def test_ensure_deal_without_deal_json_uses_folder_name(tmp_path):
    d = tmp_path / "Heron"
    d.mkdir()
    info = ingest.ensure_deal("heron", d)
    assert info["codename"] == "Project Heron"
    assert store.deal_team("heron") == []


# --------------------------------------------------------------------------- authors and kinds

def test_author_from_nearest_authors_json_then_lead(tmp_path):
    deal = tmp_path / "Falcon"
    f = touch(deal / "Models" / "Falcon LBO v2.xlsx")
    other = touch(deal / "Models" / "Unlisted.xlsx")
    (deal / "_authors.json").write_text(json.dumps({"Falcon LBO v2.xlsx": "priya@northbeam.com"}))
    info = {"lead": "david@northbeam.com", "team": ["elena@northbeam.com"]}
    assert ingest.resolve_author(f, deal, info) == "priya@northbeam.com"
    assert ingest.resolve_author(other, deal, info) == "david@northbeam.com"
    assert ingest.resolve_author(other, deal, {"team": ["elena@northbeam.com"]}) == "elena@northbeam.com"
    assert ingest.resolve_author(other, deal, None) == "elena@northbeam.com"  # falls back to the managing partner


def test_infer_doc_kind():
    k = ingest.infer_doc_kind
    assert k(Path("Falcon/Memos/Project Falcon - IC memo DRAFT v0.3.docx")) == "ic_memo"
    assert k(Path("Falcon/Decks/Falcon IC deck - outline.md")) == "deck"
    assert k(Path("Falcon/Calls/2026-10-06 Acme mgmt call.txt")) == "transcript"
    assert k(Path("Falcon/Email/RE thing.eml")) == "email"
    assert k(Path("_firm/Policies/Information barrier policy.docx")) == "policy"


def test_doc_ids_are_stable_and_prefixed():
    a = ingest.doc_id_for("Falcon/Memos/x.docx")
    assert a == ingest.doc_id_for("Falcon/Memos/x.docx")
    assert a.startswith("f_") and a != ingest.doc_id_for("Falcon/Memos/y.docx")


# --------------------------------------------------------------------------- text extraction

def test_extract_text_formats(corpus):
    root = corpus / "Northbeam Deals" / "Falcon"
    memo = ingest.extract_text(root / "Memos" / "Project Falcon - IC memo DRAFT v0.3.docx")
    assert memo["title"] == "Project Falcon - IC memo draft" and "IRR" in memo["text"]

    pdf = ingest.extract_text(root / "Diligence" / "Ardent QoE - preliminary findings.pdf")
    assert pdf["title"] and len(pdf["text"]) > 200

    eml = ingest.extract_text(root / "Email" / "RE Falcon model update - growth assumption.eml")
    assert eml["author"] == "priya@northbeam.com"
    assert eml["to"] == ["david@northbeam.com"] and eml["cc"] == ["elena@northbeam.com"]
    assert eml["date"] == "2026-10-08" and "8.0%" in eml["text"]

    call = ingest.extract_text(root / "Calls" / "2026-10-06 Acme mgmt call - CFO (transcript).txt")
    assert call["date"] == "2026-10-06" and any("Tom Becker" in p for p in call["participants"])
    assert not call["text"].startswith("Date:")

    deck = ingest.extract_text(root / "Decks" / "Falcon IC deck - outline.md")
    assert deck["title"] == "Project Falcon - IC deck outline" and not deck["text"].startswith("#")


def test_regex_quote_extraction():
    text = "Base case: 12.0% growth, FY26 revenue $53.76M, FY26 EBITDA $9.68M, exit at 11.0x, IRR 21.6%, MOIC 2.66x."
    q = {x["metric"]: x["value"] for x in ingest.regex_extract_quotes(text)}
    assert q["revenue_growth"] == pytest.approx(0.12)
    assert q["fy26_ebitda"] == pytest.approx(9.68)
    assert q["exit_multiple"] == pytest.approx(11.0)
    assert q["irr"] == pytest.approx(0.216)
    assert q["moic"] == pytest.approx(2.66)


# --------------------------------------------------------------------------- full scan

def test_scan_builds_graph_docs_and_references(deals_root, offline):
    result = ingest.scan(deals_root)
    assert not result["errors"]
    falcon = [c for c in result["commits"] if c["deal_id"] == "falcon"]
    assert len(falcon) == 5
    branches = {c["branch"] for c in falcon}
    assert "main" in branches and len(branches) >= 3  # main plus the downside and comments forks
    by_file = {Path(c["file_path"]).name: c for c in falcon}
    assert by_file["Falcon LBO v1.xlsx"]["parent_id"] is None
    assert by_file["Falcon LBO v3.xlsx"]["parent_id"] == by_file["Falcon LBO v2.xlsx"]["id"]
    assert by_file["Falcon LBO v2 - downside case (PS).xlsx"]["parent_id"] == by_file["Falcon LBO v2.xlsx"]["id"]
    assert by_file["Falcon LBO v2.xlsx"]["author_email"] == "priya@northbeam.com"

    ids = {i["id"] for i in offline}
    assert all(i.startswith("f_") for i in ids)
    assert len(result["docs"]) == 18
    # every brain document is registered in SQLite with the right wall
    for item in offline:
        doc = store.get_document(item["id"])
        assert doc is not None and doc["acl"] == item["acl"]
    memo = next(d for d in result["docs"] if d["doc_kind"] == "ic_memo" and d["deal_id"] == "falcon")
    assert store.get_document(memo["doc_id"])["acl"] == ["group:northbeam:falcon"]
    policy = next(d for d in result["docs"] if d["doc_kind"] == "policy")
    assert store.get_document(policy["doc_id"])["acl"] == ["domain:northbeam.com"]

    refs = store._rows("SELECT * FROM doc_references WHERE doc_id = ?", (memo["doc_id"],))
    assert refs and {r["quoted_commit"] for r in refs} == {by_file["Falcon LBO v2.xlsx"]["id"]}


def test_rescan_is_incremental_and_picks_up_a_new_save(deals_root, corpus, offline):
    ingest.scan(deals_root)
    offline.clear()
    again = ingest.scan(deals_root)
    assert again["commits"] == [] and again["docs"] == [] and offline == []

    live = corpus / "_live" / "Falcon" / "Models" / "Falcon LBO v4.xlsx"
    dest = deals_root / "Falcon" / "Models" / live.name
    shutil.copy2(live, dest)
    result = ingest.scan(deals_root)
    assert len(result["commits"]) == 1
    commit = result["commits"][0]
    assert commit["author_email"] == "david@northbeam.com"
    assert commit["metrics"]["exit_multiple"] == pytest.approx(9.5)
    assert [i["id"] for i in offline] == [f"f_commit_{commit['id']}"]


def test_changed_document_is_reingested_with_same_id(deals_root, offline):
    ingest.scan(deals_root)
    note = deals_root / "Falcon" / "Notes" / "slack-export-deal-falcon-2026-10-09.md"
    first_id = ingest.doc_id_for(str(note.relative_to(deals_root.resolve())))
    offline.clear()
    note.write_text(note.read_text() + "\nPriya Shah (10:02 AM): Updated.\n")
    result = ingest.scan(deals_root)
    assert [d["doc_id"] for d in result["docs"]] == [first_id]
    assert [i["id"] for i in offline] == [first_id]


def test_hydra_failure_leaves_files_unseen_for_retry(deals_root, monkeypatch):
    def boom(items):
        raise ConnectionError("down")

    monkeypatch.setattr(ingest, "hydra_ingest", boom)
    monkeypatch.setattr(ingest, "llm_extract_quotes", lambda t: [])
    result = ingest.scan(deals_root)
    assert result["errors"] and "HydraDB" in result["errors"][0]["error"]
    assert store._rows("SELECT COUNT(*) AS n FROM files_seen WHERE kind = 'doc'")[0]["n"] == 0


def test_watcher_publishes_commit_events_with_full_snapshot(deals_root, offline):
    before = watcher.latest_seq()
    watcher.scan_once(deals_root)
    events = watcher.events_since(before)
    commits = [e for e in events if e["type"] == "commit"]
    assert commits and all(e["deal_id"] and "changes" in e["commit"] for e in commits)
    assert any(e["type"] == "doc" for e in events)
    assert watcher.events_since(watcher.latest_seq()) == []


def test_reset_demo_undoes_live_save_and_allows_replay(deals_root, corpus, offline, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("reset_demo", Path(__file__).parent.parent / "scripts" / "reset_demo.py")
    reset_demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reset_demo)
    monkeypatch.setattr(ingest, "IMPORT_ROOT", deals_root)
    monkeypatch.setattr(ingest, "LIVE_ROOT", corpus / "_live")

    first = ingest.scan(deals_root)
    head_before = {c["branch"]: c["id"] for c in first["commits"] if c["deal_id"] == "falcon"}["main"]
    live = corpus / "_live" / "Falcon" / "Models" / "Falcon LBO v4.xlsx"
    shutil.copy2(live, deals_root / "Falcon" / "Models" / live.name)
    saved = ingest.scan(deals_root)["commits"][0]

    reset_demo.main()

    assert not (deals_root / "Falcon" / "Models" / live.name).exists()
    assert vcs.get_commit(saved["id"]) is None
    model = vcs.list_models("falcon")[0]
    assert model["head"] == head_before
    shutil.copy2(live, deals_root / "Falcon" / "Models" / live.name)
    replay = ingest.scan(deals_root)["commits"]
    assert [c["id"] for c in replay] == [saved["id"]]
