import json

import pytest

from app import agent, brain, ingest, retrieval, store
from tests.test_ingest import corpus, deals_root  # noqa: F401  (fixtures)

PRIYA = "priya@northbeam.com"  # falcon
MARCUS = "marcus@northbeam.com"  # harbor


@pytest.fixture(autouse=True)
def local(monkeypatch):
    monkeypatch.setenv("BRAIN_RETRIEVAL", "local")

    def no_llm(text):
        raise RuntimeError("offline")

    monkeypatch.setattr(ingest, "llm_extract_quotes", no_llm)
    # The local backend must never reach HydraDB.
    monkeypatch.setattr(ingest, "hydra_ingest", lambda items: pytest.fail("HydraDB called"))


def doc(doc_id, title, body, deal=None):
    return {"doc_id": doc_id, "title": title, "deal_id": deal, "kind": "knowledge_base", "doc_kind": "memo",
            "date": "2026-10-06", "author_email": PRIYA, "acl": ingest.deal_acl(deal), "body": body}


def add(*docs):
    for d in docs:
        ingest.register_document(d)
    retrieval.index_local(list(docs))


def test_backend_choice(monkeypatch):
    monkeypatch.delenv("BRAIN_RETRIEVAL")
    monkeypatch.delenv("HYDRA_DB_API_KEY", raising=False)
    assert retrieval.backend() == "local"
    monkeypatch.setenv("HYDRA_DB_API_KEY", "x")
    assert retrieval.backend() == "hydra"
    monkeypatch.setenv("BRAIN_RETRIEVAL", "local")
    assert retrieval.backend() == "local"


def test_chunks_split_long_text_on_paragraphs_with_overlap():
    body = "\n\n".join(f"Paragraph {i}. " + "word " * 60 for i in range(20))
    pieces = retrieval.chunks_of(body)
    assert len(pieces) > 3
    assert all(len(p) <= retrieval.CHUNK_CHARS for p in pieces)
    assert retrieval.chunks_of("short") == ["short"]
    assert retrieval.chunks_of("   ") == []
    # Every paragraph survives somewhere.
    assert all(any(f"Paragraph {i}." in p for p in pieces) for i in range(20))


def test_query_text_cannot_inject_fts_syntax():
    assert retrieval.match_expression('why did "IRR" change? OR NEAR(x') == '"irr" OR "change" OR "near"'
    assert retrieval.match_expression("the") == '"the"'
    assert retrieval.match_expression("?!") is None


def test_walls_apply_to_local_search():
    add(
        doc("firm_policy", "Leverage policy", "Firm policy caps leverage at 5.5x EBITDA."),
        doc("falcon_memo", "Falcon IC memo", "Falcon churn risk lowered EBITDA guidance.", deal="falcon"),
        doc("harbor_memo", "Harbor IC memo", "Harbor EBITDA beat the plan.", deal="harbor"),
    )
    ids = lambda email, **kw: {c["id"] for c in brain.search(email, "EBITDA", **kw)["chunks"]}  # noqa: E731
    assert ids(PRIYA) == {"firm_policy", "falcon_memo"}
    assert ids(MARCUS) == {"firm_policy", "harbor_memo"}
    assert ids("nobody@example.com") == set()
    assert ids(PRIYA, deal="falcon") == {"falcon_memo"}
    assert ids(MARCUS, deal="falcon") == set()


def test_ranking_prefers_the_document_about_the_question():
    add(
        doc("a", "Weekly notes", "Revenue was discussed briefly among many other topics. " * 3, deal="falcon"),
        doc("b", "Churn analysis", "Customer churn at NorthStar drove the churn assumption up; churn churn.", deal="falcon"),
    )
    hits = brain.search(PRIYA, "why did churn change")["chunks"]
    assert hits[0]["id"] == "b"
    assert hits[0]["url"] is None or hits[0]["url"]  # shape matches the HydraDB path
    assert set(hits[0]) == {"id", "title", "file", "url", "deal", "kind", "date", "author", "text"}


def test_unregistered_documents_are_not_returned():
    retrieval.index_local([doc("ghost", "Deleted memo", "EBITDA ghost", deal="falcon")])
    assert brain.search(PRIYA, "EBITDA ghost")["chunks"] == []


def test_reindexing_replaces_instead_of_duplicating():
    add(doc("m", "Memo", "first version about margins", deal="falcon"))
    add(doc("m", "Memo", "second version about margins", deal="falcon"))
    hits = brain.search(PRIYA, "margins")["chunks"]
    assert [h["text"] for h in hits] == ["second version about margins"]


def test_agent_tool_uses_the_local_index_with_walls():
    add(doc("falcon_memo", "Falcon IC memo", "Falcon churn risk.", deal="falcon"))
    out, summary = agent._run_tool(MARCUS, "search_brain", {"query": "churn"}, {})
    assert out["chunks"] == []
    out, summary = agent._run_tool(PRIYA, "search_brain", {"query": "churn"}, {})
    assert [c["id"] for c in out["chunks"]] == ["falcon_memo"]


def test_a_scan_indexes_locally_and_a_rebuild_restores_the_index(deals_root):  # noqa: F811
    result = ingest.scan(deals_root)
    assert not result["errors"]
    count = lambda: store._rows("SELECT COUNT(DISTINCT doc_id) AS n FROM search_chunks")[0]["n"]  # noqa: E731
    indexed = count()
    assert indexed == len({d["doc_id"] for d in result["docs"] + result["changelogs"]})
    # A change log is searchable: it names the model's cells and metrics.
    assert any(c["id"].startswith("f_commit_") for c in brain.search("elena@northbeam.com", "commit metrics revenue")["chunks"])

    store.execute("DELETE FROM search_chunks")
    rebuilt = ingest.rebuild_index(deals_root)
    assert rebuilt["missing"] == []
    assert count() == indexed
    kinds = {json.loads(r["acl"])[0] for r in store._rows("SELECT acl FROM search_chunks")}
    assert "domain:northbeam.com" in kinds and "group:northbeam:falcon" in kinds
