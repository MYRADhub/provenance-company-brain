import pytest

from app import agent, config, store, vcs

PRIYA = "priya@northbeam.com"
MARCUS = "marcus@northbeam.com"


@pytest.fixture(autouse=True)
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "brain.db")
    store.init_db(reset=True)
    store.executemany(
        "INSERT INTO users(email, name, title, role) VALUES (?,?,?,?)",
        [
            (PRIYA, "Priya Shah", "Associate", "associate"),
            (MARCUS, "Marcus Lee", "Associate", "associate"),
            ("sam@northbeam.com", "Sam Patel", "Chief Compliance Officer", "compliance"),
        ],
    )
    store.executemany(
        "INSERT INTO deals(id, codename, company, sector, status, summary) VALUES (?,?,?,?,?,?)",
        [("falcon", "Project Falcon", "Acme Freight Systems", "Logistics", "diligence", "")],
    )
    store.execute("INSERT INTO deal_members(deal_id, email) VALUES (?,?)", ("falcon", PRIYA))
    models = {"falcon": [{"id": "m_falcon", "name": "Falcon LBO"}]}
    commits = {"abc1234567": {"id": "abc1234567", "model_id": "m_falcon"}}
    monkeypatch.setattr(vcs, "list_models", lambda deal_id: models.get(deal_id, []))
    monkeypatch.setattr(vcs, "get_commit", lambda cid: commits.get(cid))
    monkeypatch.setattr(vcs, "graph", lambda model_id: {"model": {"id": model_id}, "commits": [], "branches": []})
    monkeypatch.setattr(vcs, "commit_diff", lambda a, b: [{"sheet": "Assumptions", "cell": "C7"}])
    monkeypatch.setattr(vcs, "stale_references", lambda deal_id: [{"doc_id": "memo"}])


CALLS = [
    ("list_models", {"deal_id": "falcon"}, {"deal_id": "nope"}),
    ("find_stale_references", {"deal_id": "falcon"}, {"deal_id": "nope"}),
    ("get_model_graph", {"model_id": "m_falcon"}, {"model_id": "m_nope"}),
    ("diff_commits", {"from_commit": "abc1234567", "to_commit": "abc1234567"}, {"from_commit": "zzz", "to_commit": "zzz"}),
    ("search_brain", {"query": "growth", "deal": "falcon"}, {"query": "growth", "deal": "nope"}),
]


@pytest.mark.parametrize("tool,walled_args,unknown_args", CALLS)
def test_walled_deal_is_indistinguishable_from_unknown(tool, walled_args, unknown_args):
    walled, summary = agent._run_tool(MARCUS, tool, walled_args, {})
    unknown, _ = agent._run_tool(MARCUS, tool, unknown_args, {})
    assert summary == "restricted"
    assert walled == unknown
    assert "Falcon" not in str(walled)


@pytest.mark.parametrize("tool,args", [(t, a) for t, a, _ in CALLS if t != "search_brain"])
def test_team_member_gets_data(tool, args):
    _, summary = agent._run_tool(PRIYA, tool, args, {})
    assert summary != "restricted"


def test_unknown_user_has_no_principals():
    assert store.user_principals("nobody@example.com") == []
    assert "group:northbeam:falcon" in store.user_principals(PRIYA)
    assert "group:northbeam:falcon" not in store.user_principals(MARCUS)
