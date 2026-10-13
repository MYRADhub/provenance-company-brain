import json
from types import SimpleNamespace

import pytest

from app import agent, governance, llm, vcs

# conftest stubs this per test; keep the real one to check its fallback.
REAL_COMMIT_MESSAGE = vcs.llm_commit_message
MARCUS = "marcus@northbeam.com"
PRIYA = "priya@northbeam.com"


@pytest.fixture
def no_keys(monkeypatch):
    for var in ("BRAIN_LLM_PROVIDER", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(var, raising=False)


def test_provider_choice(no_keys, monkeypatch):
    assert llm.provider() == "none"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert llm.provider() == "anthropic"
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    assert llm.provider() == "openai"
    monkeypatch.setenv("BRAIN_LLM_PROVIDER", "anthropic")
    assert llm.provider() == "anthropic"


class FakeAnthropic:
    """Answers messages.create from a script and keeps every request."""

    def __init__(self, replies):
        self.replies = iter(replies)
        self.requests = []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kwargs):
        # Copy the turn list: the loop appends to it after this call returns.
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})
        return next(self.replies)


def text(t):
    return SimpleNamespace(type="text", text=t)


@pytest.fixture
def claude(monkeypatch):
    def use(replies):
        fake = FakeAnthropic(replies)
        monkeypatch.setenv("BRAIN_LLM_PROVIDER", "anthropic")
        monkeypatch.setattr(llm, "_anthropic_client", lambda: fake)
        return fake

    return use


def test_claude_tool_loop_returns_results_and_keeps_thinking_with_its_call(claude):
    thinking = SimpleNamespace(type="thinking", thinking="", signature="sig")
    use = SimpleNamespace(type="tool_use", id="tu_1", name="list_my_deals", input={})
    fake = claude([
        SimpleNamespace(stop_reason="tool_use", content=[thinking, use]),
        SimpleNamespace(stop_reason="end_turn", content=[text("You are on Project Falcon.")]),
    ])
    calls = []
    answer = llm.run_tools("SYSTEM", [{"role": "user", "content": "my deals?"}],
                           [{"name": "list_my_deals", "description": "d", "input_schema": {"type": "object"}}],
                           lambda name, args: calls.append((name, args)) or {"deals": ["falcon"]}, 5)
    assert answer == "You are on Project Falcon."
    assert calls == [("list_my_deals", {})]
    first, second = fake.requests
    assert first["model"] == "claude-opus-5-5" and first["system"] == "SYSTEM"
    assert first["output_config"] == {"effort": "low"} and "thinking" not in first
    assert first["tools"][0]["input_schema"] == {"type": "object"}
    assert second["messages"][1] == {"role": "assistant", "content": [thinking, use]}
    assert second["messages"][2] == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu_1", "content": json.dumps({"deals": ["falcon"]})}]}


def test_claude_refusal_without_text(claude):
    claude([SimpleNamespace(stop_reason="refusal", content=[])])
    assert llm.run_tools("S", [{"role": "user", "content": "x"}], [], lambda n, a: {}, 3) == "I can't help with that request."


def test_claude_structured_output(claude):
    fake = claude([SimpleNamespace(stop_reason="end_turn", content=[text('{"quotes": [{"metric": "irr", "value": 0.2}]}')])])
    schema = {"type": "object"}
    assert llm.complete_json("memo", schema, "quoted_metrics", instructions="extract") == {"quotes": [{"metric": "irr", "value": 0.2}]}
    req = fake.requests[0]
    assert req["output_config"] == {"effort": "low", "format": {"type": "json_schema", "schema": schema}}
    assert req["system"] == "extract"


def test_claude_truncated_structured_output_is_an_error(claude):
    claude([SimpleNamespace(stop_reason="max_tokens", content=[text('{"quo')])])
    with pytest.raises(RuntimeError):
        llm.complete_json("memo", {"type": "object"}, "q")


def test_the_brain_assistant_runs_on_claude_with_walls(claude):
    use = SimpleNamespace(type="tool_use", id="tu_1", name="list_models", input={"deal_id": "falcon"})
    claude([
        SimpleNamespace(stop_reason="tool_use", content=[use]),
        SimpleNamespace(stop_reason="end_turn", content=[text("Nothing you are cleared to see.")]),
    ])
    result = agent.run(MARCUS, [{"role": "user", "content": "What models does Falcon have?"}])
    assert result["answer"] == "Nothing you are cleared to see."
    assert result["tool_trace"] == [{"tool": "list_models", "input": {"deal_id": "falcon"}, "summary": "restricted"}]
    assert governance.audit_log()[0]["action"] == "blocked"


def test_without_a_model_the_assistant_says_so_and_helpers_fall_back(no_keys):
    result = agent.run(PRIYA, [{"role": "user", "content": "hello"}])
    assert "no language model is configured" in result["answer"]
    assert governance.audit_log()[0]["action"] == "question"
    assert REAL_COMMIT_MESSAGE([{"sheet": "A", "cell": "B1", "label": "", "old_value": 1, "new_value": 2,
                                    "old_formula": None, "new_formula": None}], {}, {}, "main", False) is None
