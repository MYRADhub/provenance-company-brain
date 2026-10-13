"""The language model behind the brain's own assistant and helpers: OpenAI or Claude.

BRAIN_LLM_PROVIDER picks one (`openai` or `anthropic`); unset, it is OpenAI when OPENAI_API_KEY is set,
else Claude when ANTHROPIC_API_KEY is set, else none (every LLM step then uses its fallback: heuristic
metric cells, a plain commit message, regex quote extraction, and an assistant that says it is off).

Models: OPENAI_MODEL (default gpt-5.5) and ANTHROPIC_MODEL (default claude-opus-5-5). Every call is bounded
by a timeout and one retry so a hung request fails fast instead of leaving an alert pending.
"""

import json
import os
from collections.abc import Callable

TIMEOUT_S = 60
OPENAI_DEFAULT = "gpt-5.5"
ANTHROPIC_DEFAULT = "claude-opus-5-5"
# The assistant answers in a few sentences; tool results go in, not out.
ANTHROPIC_MAX_TOKENS = 8000


class LlmUnavailable(RuntimeError):
    pass


def provider() -> str:
    chosen = os.environ.get("BRAIN_LLM_PROVIDER", "").strip().lower()
    if chosen in ("openai", "anthropic"):
        return chosen
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return "anthropic"
    return "none"


def available() -> bool:
    return provider() != "none"


def _openai_client():
    from openai import OpenAI

    return OpenAI(timeout=TIMEOUT_S, max_retries=1)


def _anthropic_client():
    import anthropic

    return anthropic.Anthropic(timeout=TIMEOUT_S, max_retries=1)


def _openai_model() -> str:
    return os.environ.get("OPENAI_MODEL", OPENAI_DEFAULT)


def _anthropic_model() -> str:
    return os.environ.get("ANTHROPIC_MODEL", ANTHROPIC_DEFAULT)


def _anthropic_text(response) -> str:
    return "".join(b.text for b in response.content if b.type == "text").strip()


def complete(prompt: str, instructions: str | None = None) -> str:
    """One short answer to one prompt. Raises on any failure; callers keep their fallback."""
    which = provider()
    if which == "openai":
        resp = _openai_client().responses.create(
            model=_openai_model(), reasoning={"effort": "low"}, input=prompt,
            **({"instructions": instructions} if instructions else {}),
        )
        return resp.output_text or ""
    if which == "anthropic":
        resp = _anthropic_client().messages.create(
            model=_anthropic_model(), max_tokens=ANTHROPIC_MAX_TOKENS, output_config={"effort": "low"},
            messages=[{"role": "user", "content": prompt}], **({"system": instructions} if instructions else {}),
        )
        return _anthropic_text(resp)
    raise LlmUnavailable("No language model is configured.")


def complete_json(prompt: str, schema: dict, name: str, instructions: str | None = None) -> dict:
    """An answer that follows a JSON schema (strict on both providers). Raises on any failure."""
    which = provider()
    if which == "openai":
        resp = _openai_client().responses.create(
            model=_openai_model(), reasoning={"effort": "low"}, input=prompt,
            text={"format": {"type": "json_schema", "name": name, "schema": schema, "strict": True}},
            **({"instructions": instructions} if instructions else {}),
        )
        return json.loads(resp.output_text)
    if which == "anthropic":
        resp = _anthropic_client().messages.create(
            model=_anthropic_model(), max_tokens=ANTHROPIC_MAX_TOKENS,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": prompt}], **({"system": instructions} if instructions else {}),
        )
        if resp.stop_reason in ("refusal", "max_tokens"):
            raise RuntimeError(f"No usable answer ({resp.stop_reason}).")
        return json.loads(_anthropic_text(resp))
    raise LlmUnavailable("No language model is configured.")


# A tool call the loop hands to the caller: (name, arguments) -> JSON-able result.
ToolCall = Callable[[str, dict], object]


def run_tools(instructions: str, messages: list[dict], tools: list[dict], call: ToolCall, max_steps: int) -> str:
    """Let the model call tools until it answers. `messages` are plain-text [{role, content}] turns;
    `tools` use the Anthropic shape (name, description, input_schema). Returns the final text, or ""
    when it ran out of steps."""
    which = provider()
    if which == "openai":
        return _run_openai(instructions, messages, tools, call, max_steps)
    if which == "anthropic":
        return _run_anthropic(instructions, messages, tools, call, max_steps)
    raise LlmUnavailable("No language model is configured.")


def _run_openai(instructions, messages, tools, call, max_steps) -> str:
    client = _openai_client()
    items: list = [{"role": m["role"], "content": m["content"]} for m in messages if m.get("content")]
    specs = [{"type": "function", "name": t["name"], "description": t["description"], "parameters": t["input_schema"]}
             for t in tools]
    for _ in range(max_steps):
        response = client.responses.create(model=_openai_model(), instructions=instructions, input=items, tools=specs,
                                           reasoning={"effort": "low"})
        calls = [o for o in response.output if o.type == "function_call"]
        if not calls:
            return response.output_text or ""
        items += response.output  # keeps reasoning items paired with their function calls
        for c in calls:
            try:
                args = json.loads(c.arguments or "{}")
            except ValueError:
                args = {}
            items.append({"type": "function_call_output", "call_id": c.call_id,
                          "output": json.dumps(call(c.name, args), default=str)})
    return ""


def _run_anthropic(instructions, messages, tools, call, max_steps) -> str:
    client = _anthropic_client()
    turns: list = [{"role": m["role"], "content": m["content"]} for m in messages if m.get("content")]
    for _ in range(max_steps):
        response = client.messages.create(model=_anthropic_model(), max_tokens=ANTHROPIC_MAX_TOKENS, system=instructions,
                                          tools=tools, messages=turns, output_config={"effort": "low"})
        uses = [b for b in response.content if b.type == "tool_use"]
        if response.stop_reason != "tool_use" or not uses:
            text = _anthropic_text(response)
            if response.stop_reason == "refusal" and not text:
                return "I can't help with that request."
            return text
        # The whole assistant turn goes back unchanged: thinking blocks must stay paired with their tool calls.
        turns.append({"role": "assistant", "content": response.content})
        results = []
        for use in uses:
            args = use.input if isinstance(use.input, dict) else {}
            results.append({"type": "tool_result", "tool_use_id": use.id,
                            "content": json.dumps(call(use.name, args), default=str)})
        turns.append({"role": "user", "content": results})
    return ""
