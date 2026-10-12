from app import store

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


def test_brain_drops_results_not_registered_in_sqlite(monkeypatch):
    from types import SimpleNamespace

    from app import brain

    store.execute(
        "INSERT INTO documents(doc_id, title, deal_id, kind, date, author_email, acl_json) VALUES (?,?,?,?,?,?,?)",
        ("falcon_ic_deck", "Project Falcon - IC deck outline", "falcon", "deck", "2026-10-04", PRIYA,
         '["group:northbeam:falcon"]'),
    )

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
