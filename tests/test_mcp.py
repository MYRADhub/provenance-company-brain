import json

import pytest
from fastapi.testclient import TestClient

from app import brain, governance, main, store, tokens, vcs

PRIYA = "priya@northbeam.com"
MARCUS = "marcus@northbeam.com"


@pytest.fixture(autouse=True)
def fake_history(monkeypatch):
    models = {"falcon": [{"id": "m_falcon", "name": "Falcon LBO"}]}
    commits = {"abc1234567": {"id": "abc1234567", "model_id": "m_falcon"}}
    monkeypatch.setattr(vcs, "list_models", lambda deal_id: models.get(deal_id, []))
    monkeypatch.setattr(vcs, "get_commit", lambda cid: commits.get(cid))
    monkeypatch.setattr(vcs, "commit_diff", lambda a, b: [{"sheet": "Assumptions", "cell": "C7"}])
    monkeypatch.setattr(brain, "source_link", lambda doc_id: (f"Falcon/{doc_id}.xlsx", f"/files/{doc_id}"))


@pytest.fixture
def client():
    # Without a `with` block the app's lifespan (folder watcher) does not start.
    return TestClient(main.app)


def rpc(client, token, method, params=None, id_=1, headers=None):
    body = {"jsonrpc": "2.0", "method": method, **({"id": id_} if id_ is not None else {}), **({"params": params} if params else {})}
    h = {"authorization": f"Bearer {token}", **(headers or {})}
    return client.post("/mcp", content=json.dumps(body), headers=h)


def call(client, token, name, args):
    res = rpc(client, token, "tools/call", {"name": name, "arguments": args})
    assert res.status_code == 200
    return res.json()["result"]


def test_handshake_and_tool_list(client):
    token = tokens.create(PRIYA)
    init = rpc(client, token, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "Tessel"}})
    assert init.json()["result"]["protocolVersion"] == "2025-06-18"
    assert init.json()["result"]["serverInfo"]["name"] == "Provenance company brain"
    assert rpc(client, token, "notifications/initialized", id_=None).status_code == 202
    names = [t["name"] for t in rpc(client, token, "tools/list").json()["result"]["tools"]]
    assert names == ["search_brain", "list_my_deals", "list_models", "get_model_graph", "diff_commits", "find_stale_references"]


def test_unsupported_protocol_version_gets_the_latest(client):
    token = tokens.create(PRIYA)
    res = rpc(client, token, "initialize", {"protocolVersion": "1999-01-01"})
    assert res.json()["result"]["protocolVersion"] == "2025-06-18"


@pytest.mark.parametrize("auth", [None, "Bearer nope", "Bearer pb_wrong", "Basic abc"])
def test_requires_a_valid_token(client, auth):
    headers = {"authorization": auth} if auth else {}
    res = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=headers)
    assert res.status_code == 401
    assert res.headers["www-authenticate"].startswith("Bearer")


def test_identity_comes_from_the_token_not_the_arguments(client):
    token = tokens.create(MARCUS)
    # Marcus is not staffed on Falcon; naming another user in the arguments changes nothing.
    result = call(client, token, "list_models", {"deal_id": "falcon", "user": PRIYA})
    assert json.loads(result["content"][0]["text"])["error"] == "restricted"
    deals = json.loads(call(client, token, "list_my_deals", {})["content"][0]["text"])["deals"]
    assert [d["id"] for d in deals] == ["harbor"]


def test_walled_deal_looks_like_an_unknown_one(client):
    token = tokens.create(MARCUS)
    walled = call(client, token, "find_stale_references", {"deal_id": "falcon"})
    unknown = call(client, token, "find_stale_references", {"deal_id": "nope"})
    assert walled == unknown
    assert "Falcon" not in json.dumps(walled)


def test_blocked_and_allowed_calls_are_audited(client):
    call(client, tokens.create(MARCUS), "list_models", {"deal_id": "falcon"})
    call(client, tokens.create(PRIYA), "list_models", {"deal_id": "falcon"})
    log = [(e["email"], e["action"], e["outcome"]) for e in governance.audit_log()]
    assert (MARCUS, "blocked", "blocked") in log
    assert (PRIYA, "mcp_tool", "ok") in log


def test_links_are_absolute(client):
    result = call(client, tokens.create(PRIYA), "diff_commits", {"from_commit": "abc1234567", "to_commit": "abc1234567"})
    out = json.loads(result["content"][0]["text"])
    assert out["from"]["url"] == "http://testserver/files/f_commit_abc1234567"
    assert out["changes"] == [{"sheet": "Assumptions", "cell": "C7"}]


def test_tool_failure_is_a_tool_error(client, monkeypatch):
    def boom(_deal):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(vcs, "stale_references", boom)
    result = call(client, tokens.create(PRIYA), "find_stale_references", {"deal_id": "falcon"})
    assert result["isError"] is True
    assert "disk gone" in result["content"][0]["text"]


def test_unknown_tool_and_method(client):
    token = tokens.create(PRIYA)
    assert call(client, token, "drop_tables", {})["isError"] is True
    assert rpc(client, token, "resources/list").json()["error"]["code"] == -32601


def test_malformed_requests(client):
    token = tokens.create(PRIYA)
    h = {"authorization": f"Bearer {token}"}
    assert client.post("/mcp", content="{not json", headers=h).json()["error"]["code"] == -32700
    assert client.post("/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}], headers=h).json()["error"]["code"] == -32600
    bad_args = rpc(client, token, "tools/call", {"name": "list_models", "arguments": "falcon"})
    assert bad_args.json()["error"]["code"] == -32602


@pytest.mark.parametrize(
    "origin,status",
    [("http://localhost:5173", 200), ("tauri://localhost", 200), ("http://tauri.localhost", 200), ("https://evil.example", 403)],
)
def test_origin_check(client, origin, status):
    assert rpc(client, tokens.create(PRIYA), "ping", headers={"origin": origin}).status_code == status


def test_extra_origins_from_env(client, monkeypatch):
    monkeypatch.setenv("MCP_ALLOWED_ORIGINS", "https://tessel.example.com")
    assert rpc(client, tokens.create(PRIYA), "ping", headers={"origin": "https://tessel.example.com"}).status_code == 200


def test_no_event_stream(client):
    assert client.get("/mcp").status_code == 405


def test_tokens_are_stored_hashed_and_revocable():
    token = tokens.create(PRIYA, "laptop")
    assert token.startswith("pb_")
    assert not store._rows("SELECT 1 FROM api_tokens WHERE token_hash = ?", (token,))
    assert tokens.user_for(token) == PRIYA
    [listed] = tokens.list_for(PRIYA)
    assert listed["label"] == "laptop" and listed["last_used_at"]
    assert not tokens.revoke(MARCUS, listed["id"])
    assert tokens.revoke(PRIYA, listed["id"])
    assert tokens.user_for(token) is None


def test_token_stops_working_when_its_user_is_removed():
    token = tokens.create(PRIYA)
    store.execute("DELETE FROM users WHERE email = ?", (PRIYA,))
    assert tokens.user_for(token) is None


def test_tokens_only_for_known_users():
    with pytest.raises(ValueError):
        tokens.create("nobody@example.com")
