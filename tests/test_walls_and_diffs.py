from app import agent, store
from app.excel_diff import diff_workbooks

PRIYA = "priya@northbeam.com"
MARCUS = "marcus@northbeam.com"
SAM = "sam@northbeam.com"


def test_principals_follow_deal_membership():
    assert "group:northbeam:falcon" in store.user_principals(PRIYA)
    assert "group:northbeam:falcon" not in store.user_principals(MARCUS)
    assert store.user_principals("nobody@example.com") == []


def test_can_access_deal():
    assert store.can_access_deal(PRIYA, "falcon")
    assert not store.can_access_deal(MARCUS, "falcon")
    assert not store.can_access_deal(SAM, "harbor")
    assert not store.can_access_deal(PRIYA, "does-not-exist")


def test_database_tools_refuse_walled_deal_without_revealing_it():
    sources = {}
    for tool, args in [
        ("get_model_history", {"deal_id": "falcon"}),
        ("get_model_changes", {"deal_id": "falcon", "from_version": 1, "to_version": 3}),
        ("find_stale_references", {"deal_id": "falcon"}),
        ("search_brain", {"query": "growth", "deal": "falcon"}),
    ]:
        walled, summary = agent._run_tool(MARCUS, tool, args, sources)
        unknown, _ = agent._run_tool(MARCUS, tool, {**args, "deal_id": "nope", "deal": "nope"}, sources)
        assert summary == "restricted"
        assert walled == unknown  # a walled deal is indistinguishable from a nonexistent one
        assert "Falcon" not in str(walled)


def test_changes_span_multiple_versions():
    changes = store.get_changes("falcon", 1, 3)
    growth = next(c for c in changes if c["sheet"] == "Assumptions" and c["cell"] == "B3")
    assert growth["old_value"] == 0.15 and growth["new_value"] == 0.08


def test_stale_references_compare_against_latest():
    stale = store.stale_references("falcon")
    assert stale
    assert all(s["quoted_value"] != s["latest_value"] for s in stale)
    assert {s["doc_id"] for s in stale} <= {"falcon_ic_memo_draft", "falcon_ic_deck"}


def test_diff_workbooks_identical_is_empty():
    v = store.list_versions("falcon")
    assert diff_workbooks(v[0]["file_path"], v[0]["file_path"]) == []


def test_diff_workbooks_detects_labelled_cell_change():
    v = store.list_versions("falcon")
    diff = diff_workbooks(v[1]["file_path"], v[2]["file_path"])
    labels = {d["label"] for d in diff}
    assert any("Growth" in l for l in labels)


def test_brain_drops_results_not_registered_in_sqlite(monkeypatch):
    from types import SimpleNamespace

    from app import brain

    def chunk(doc_id):
        return SimpleNamespace(id=doc_id, chunk_content="x", source_title="t", source_type="message",
                               metadata={}, additional_metadata={})

    fake = SimpleNamespace(query=lambda **_: SimpleNamespace(data=SimpleNamespace(
        chunks=[chunk("falcon_model_changelog_v99"), chunk("falcon_ic_deck")], graph_context=None)))
    monkeypatch.setattr(brain, "hydra", lambda: fake)
    ids = [c["id"] for c in brain.search(PRIYA, "anything")["chunks"]]
    assert ids == ["falcon_ic_deck"]


def test_brain_returns_nothing_for_unknown_user():
    from app import brain

    assert brain.search("nobody@example.com", "anything") == {"chunks": [], "graph_paths": []}
