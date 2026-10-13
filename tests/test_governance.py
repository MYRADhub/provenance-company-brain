import pytest

from app import agent, governance, store

ELENA = "elena@northbeam.com"
SAM = "sam@northbeam.com"
PRIYA = "priya@northbeam.com"
MARCUS = "marcus@northbeam.com"


def test_only_admins_manage_access():
    assert governance.is_admin(ELENA) and governance.is_admin(SAM)
    assert not governance.is_admin(PRIYA)
    with pytest.raises(governance.GovernanceError):
        governance.overview(PRIYA)
    with pytest.raises(governance.GovernanceError):
        governance.set_member(PRIYA, "falcon", MARCUS, True)


def test_staffing_change_applies_to_principals_immediately():
    assert "group:northbeam:falcon" not in store.user_principals(MARCUS)
    governance.set_member(SAM, "falcon", MARCUS, True)
    assert store.can_access_deal(MARCUS, "falcon")
    assert "group:northbeam:falcon" in store.user_principals(MARCUS)
    governance.set_member(SAM, "falcon", MARCUS, False)
    assert not store.can_access_deal(MARCUS, "falcon")
    actions = [e["action"] for e in governance.audit_log()]
    assert actions.count("staffing") == 2


def test_overview_lists_teams():
    data = governance.overview(SAM)
    falcon = next(d for d in data["deals"] if d["id"] == "falcon")
    assert PRIYA in falcon["team"] and MARCUS not in falcon["team"]


def test_access_request_flow():
    with pytest.raises(governance.GovernanceError):
        governance.request_access(MARCUS, "   ")
    req = governance.request_access(MARCUS, "Need the logistics deal model for a comps exercise")
    assert req["status"] == "pending"
    assert governance.my_requests(MARCUS)[0]["status"] == "pending"
    with pytest.raises(governance.GovernanceError):
        governance.decide(SAM, req["id"], True)  # must pick a deal
    decided = governance.decide(SAM, req["id"], True, "falcon")
    assert decided["status"] == "approved" and store.can_access_deal(MARCUS, "falcon")
    with pytest.raises(governance.GovernanceError):
        governance.decide(SAM, req["id"], False)


def test_declined_request_grants_nothing():
    req = governance.request_access(MARCUS, "Falcon please")
    governance.decide(ELENA, req["id"], False)
    assert not store.can_access_deal(MARCUS, "falcon")


def test_questions_and_blocked_lookups_are_audited(monkeypatch):
    from types import SimpleNamespace

    call = SimpleNamespace(type="function_call", name="list_models", arguments='{"deal_id": "falcon"}', call_id="c1")
    replies = iter([
        SimpleNamespace(output=[call], output_text=""),
        SimpleNamespace(output=[], output_text="Nothing you are cleared to see."),
    ])
    fake = SimpleNamespace(responses=SimpleNamespace(create=lambda **_: next(replies)))
    monkeypatch.setattr(agent, "OpenAI", lambda **_: fake)

    result = agent.run(MARCUS, [{"role": "user", "content": "Tell me about Falcon"}])

    assert result["tool_trace"][0]["summary"] == "restricted"
    entries = governance.audit_log()
    assert [e["action"] for e in entries[:3]] == ["blocked", "blocked", "question"]
    assert entries[0]["detail"].startswith("list_models")  # the agent's blocked lookup
    assert "Project Falcon" in entries[1]["detail"]  # the question named a walled deal
    assert "Falcon" in entries[2]["detail"] and all(e["email"] == MARCUS for e in entries[:3])


def test_asking_about_a_walled_deal_is_logged_as_blocked(monkeypatch):
    from types import SimpleNamespace

    fake = SimpleNamespace(responses=SimpleNamespace(create=lambda **_: SimpleNamespace(output=[], output_text="Nothing you are cleared to see.")))
    monkeypatch.setattr(agent, "OpenAI", lambda **_: fake)

    agent.run(MARCUS, [{"role": "user", "content": "Tell me about Project Falcon and Acme Freight"}])
    entries = governance.audit_log()
    assert entries[0]["outcome"] == "blocked" and "Project Falcon" in entries[0]["detail"]

    agent.run(PRIYA, [{"role": "user", "content": "Tell me about Project Falcon"}])
    assert governance.audit_log()[0]["action"] == "question"  # staffed: no blocked entry


def test_walled_deal_matching_needs_whole_words():
    assert governance.walled_deals_mentioned(MARCUS, "falconry is a hobby") == []
    assert [d["id"] for d in governance.walled_deals_mentioned(MARCUS, "what about falcon?")] == ["falcon"]
