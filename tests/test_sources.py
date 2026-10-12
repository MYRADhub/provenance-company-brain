import json

from fastapi.testclient import TestClient

from app import brain, ingest, main, store

PRIYA = "priya@northbeam.com"
MARCUS = "marcus@northbeam.com"


def _register(tmp_path, monkeypatch, doc_id="f_test", deal="falcon"):
    monkeypatch.setattr(ingest, "IMPORT_ROOT", tmp_path)
    f = tmp_path / "Falcon" / "Calls" / "call.txt"
    f.parent.mkdir(parents=True)
    f.write_text("CFO says two customers may churn")
    store.execute("INSERT INTO documents(doc_id, title, deal_id, kind, date, author_email, acl_json) VALUES (?,?,?,?,?,?,?)",
                  (doc_id, "CFO call", deal, "transcript", "2026-10-06", PRIYA, json.dumps([])))
    store.execute("INSERT INTO files_seen(path, sha256, mtime, kind, doc_id, commit_id) VALUES (?,?,?,?,?,?)",
                  ("Falcon/Calls/call.txt", "x", 0, "doc", doc_id, None))


def test_source_link_for_files_and_commits(tmp_path, monkeypatch):
    _register(tmp_path, monkeypatch)
    assert brain.source_link("f_test") == ("Falcon/Calls/call.txt", "/files/f_test")
    assert brain.source_link("f_commit_abc")[1] == "#commit=abc"
    assert brain.source_link("f_model_m1") == (None, "#model=m1")
    assert brain.source_link("unknown") == (None, None)


def test_file_endpoint_respects_walls(tmp_path, monkeypatch):
    _register(tmp_path, monkeypatch)
    client = TestClient(main.app)
    ok = client.get("/files/f_test", params={"user": PRIYA})
    assert ok.status_code == 200 and "churn" in ok.text
    walled = client.get("/files/f_test", params={"user": MARCUS})
    missing = client.get("/files/nope", params={"user": MARCUS})
    assert walled.status_code == missing.status_code == 404
    assert walled.json() == missing.json()


def test_answer_links_are_verified(monkeypatch):
    from app import agent, vcs

    monkeypatch.setattr(vcs, "get_commit", lambda cid: {"id": "abc123", "model_id": "m"} if cid == "abc123" else None)
    sources = {"f_test": {"id": "f_test", "title": "CFO call", "kind": "transcript", "url": "/files/f_test", "file": "Falcon/Calls/call.txt"}}
    text = "See [CFO call](/files/f_test), [v3](#commit=abc123), [made up](/files/f_fake) and [bad](#commit=zzz)."
    answer, cited = agent._verified_links(text, sources)
    assert "[CFO call](/files/f_test)" in answer and "[v3](#commit=abc123)" in answer
    assert "f_fake" not in answer and "made up" in answer and "#commit=zzz" not in answer
    assert [c["url"] for c in cited] == ["/files/f_test", "#commit=abc123"]
