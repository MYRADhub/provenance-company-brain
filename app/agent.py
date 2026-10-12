"""LLM agent over two sources: SQLite facts (what changed) and the HydraDB brain (why)."""

import json

from openai import OpenAI

from app import brain, store
from app.config import LLM_MODEL

TODAY = "Monday, October 12, 2026"
MAX_STEPS = 10

TOOLS = [
    {
        "name": "search_brain",
        "description": (
            "Semantic + graph search over the firm's unstructured knowledge: memos, call transcripts, "
            "emails, chat notes, policies, precedents, expertise directory, and model change logs. "
            "This is where the reasons behind decisions and number changes live. Results are already "
            "filtered to what the current user is cleared to see. Call it several times with different "
            "phrasings when the first search is thin."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language search query."},
                "deal": {
                    "type": "string",
                    "description": "Optional deal id to restrict results to one deal (e.g. 'falcon'), or 'firm' for firm-wide documents.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_my_deals",
        "description": "List the deals the current user is cleared to see, with codename, company, sector and status.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_model_history",
        "description": (
            "Structured facts from the database: every saved version of a deal's financial model with author, "
            "timestamp and key metrics (revenue growth, EBITDA margin, FY26 revenue and EBITDA, exit multiple, IRR, MOIC)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"deal_id": {"type": "string"}},
            "required": ["deal_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_model_changes",
        "description": (
            "Cell-level diff between two versions of a deal's financial model (sheet, cell, label, old and new value). "
            "Tells you exactly what changed, but not why; use search_brain for the why."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "deal_id": {"type": "string"},
                "from_version": {"type": "integer"},
                "to_version": {"type": "integer"},
            },
            "required": ["deal_id", "from_version", "to_version"],
            "additionalProperties": False,
        },
    },
    {
        "name": "find_stale_references",
        "description": (
            "Find documents (IC memos, decks) that quote a model metric whose value has since changed in the latest "
            "model version. Returns quoted value, quoted version, latest value and the document owner."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"deal_id": {"type": "string"}},
            "required": ["deal_id"],
            "additionalProperties": False,
        },
    },
]


def _system_prompt(email: str) -> str:
    user = store.get_user(email) or {}
    deals = store.accessible_deals(email)
    compliance = store.compliance_contact() or {}
    deal_lines = "\n".join(
        f"- {d['id']}: {d['codename']} ({d['company']}, {d['sector']}, {d['status']})" for d in deals
    ) or "- none"
    return f"""You are Provenance, the company brain of Northbeam Capital, a mid-market private equity fund.
Today is {TODAY}.

You are answering for {user.get('name')} ({user.get('title')}, {email}).
Deals this user is cleared for:
{deal_lines}

You have two kinds of tools:
- Database tools (get_model_history, get_model_changes, find_stale_references, list_my_deals) return hard facts: what changed, when, by whom.
- search_brain returns unstructured knowledge: the reasons, context, decisions and people behind those facts.
Strong answers join them: take the fact from the database, then find the reason in the brain and cite it.

Rules:
- Ground every claim in tool results. Cite sources inline by document title in italics, e.g. *Management call - Acme Freight CFO (Oct 6)*.
- When numbers changed, give old -> new values, the version, the author and the date, then the reason with its source.
- Information barriers are enforced. If a tool returns "restricted" or a search finds nothing relevant, do not guess or hint at what might exist.
  Say that you have nothing the user is cleared to see on that topic and that, if it relates to a walled deal,
  they should contact {compliance.get('name', 'Compliance')} ({compliance.get('email', '')}). Never confirm or deny that a restricted deal exists.
- Be concise and decision-oriented: lead with the answer, then the evidence. Use short Markdown sections or bullets and bold the key numbers.
- When you notice something the user should act on (stale numbers in a deck, a contradiction with a memo, someone to loop in), say so explicitly under **Actions**."""


def _restricted() -> dict:
    c = store.compliance_contact() or {}
    return {
        "error": "restricted",
        "message": f"No accessible records. If this relates to a walled deal, contact {c.get('name')} ({c.get('email')}).",
    }


def _run_tool(email: str, name: str, args: dict, sources: dict) -> tuple[dict, str]:
    if name == "search_brain":
        deal = args.get("deal") or None
        if deal and deal != "firm" and not store.can_access_deal(email, deal):
            return _restricted(), "restricted"
        res = brain.search(email, args["query"], deal=deal)
        for ch in res["chunks"]:
            if ch["id"] and ch["id"] not in sources:
                sources[ch["id"]] = {"id": ch["id"], "title": ch["title"], "kind": ch["kind"], "deal": ch["deal"]}
        titles = list(dict.fromkeys(c["title"] for c in res["chunks"] if c["title"]))
        return res, f"{len(res['chunks'])} passages from {len(titles)} documents" + (
            f": {', '.join(titles[:4])}" if titles else ""
        )

    if name == "list_my_deals":
        deals = store.accessible_deals(email)
        return {"deals": deals}, f"{len(deals)} deals"

    deal_id = args.get("deal_id", "")
    if not store.can_access_deal(email, deal_id):
        return _restricted(), "restricted"

    if name == "get_model_history":
        versions = store.list_versions(deal_id)
        return {"deal_id": deal_id, "versions": versions}, f"{len(versions)} versions"
    if name == "get_model_changes":
        changes = store.get_changes(deal_id, int(args["from_version"]), int(args["to_version"]))
        return {"changes": changes}, f"{len(changes)} cell changes v{args['from_version']} -> v{args['to_version']}"
    if name == "find_stale_references":
        stale = store.stale_references(deal_id)
        return {"stale": stale}, f"{len(stale)} stale references"

    return {"error": f"unknown tool {name}"}, "error"


def _openai_tools() -> list[dict]:
    return [
        {"type": "function", "name": t["name"], "description": t["description"], "parameters": t["input_schema"]}
        for t in TOOLS
    ]


def run(email: str, messages: list[dict]) -> dict:
    """Run the agent loop. `messages` is the chat history as [{role, content}] with plain-text content."""
    client = OpenAI()
    items: list = [{"role": m["role"], "content": m["content"]} for m in messages if m.get("content")]
    tools = _openai_tools()
    instructions = _system_prompt(email)
    trace: list[dict] = []
    sources: dict[str, dict] = {}
    answer = ""

    for _ in range(MAX_STEPS):
        response = client.responses.create(
            model=LLM_MODEL,
            instructions=instructions,
            input=items,
            tools=tools,
            reasoning={"effort": "low"},
        )
        calls = [o for o in response.output if o.type == "function_call"]
        if not calls:
            answer = response.output_text or ""
            break

        items += response.output  # keeps reasoning items paired with their function calls
        for call in calls:
            try:
                args = json.loads(call.arguments or "{}")
                output, summary = _run_tool(email, call.name, args, sources)
            except Exception as e:  # surfaced to the model so it can recover
                args = {}
                output, summary = {"error": str(e)}, f"error: {e}"
            trace.append({"tool": call.name, "input": args, "summary": summary})
            items.append({"type": "function_call_output", "call_id": call.call_id, "output": json.dumps(output, default=str)})
    else:
        answer = answer or "I ran out of steps before finishing. Try a narrower question."

    cited = [s for s in sources.values() if s["title"] and s["title"] in answer]
    if not cited:
        cited = list(sources.values())[:4]
    return {"answer": answer, "tool_trace": trace, "citations": cited}


def proactive_alert(email: str, deal_id: str, saved: dict) -> dict:
    """After a new model version lands, have the agent assess its impact for this user."""
    changes = "\n".join(
        f"- {c['sheet']}!{c['cell']} {c['label']}: {c['old_value']} -> {c['new_value']}" for c in saved.get("changes", [])
    )
    prompt = f"""[Automated trigger] Version v{saved['version']} of the {deal_id} model was just saved by {saved.get('author')} at {saved.get('saved_at')}.
Cell changes versus the prior version:
{changes or '- none detected'}

Write a short proactive alert for me:
1. What changed and the impact on returns (use the model history).
2. Why it changed, with the source from the brain.
3. Which documents now quote stale numbers and who owns them.
4. Any conflict with the IC memo or firm policy, and who should be looped in before IC.
Keep it tight; this is a notification, not a report."""
    return run(email, [{"role": "user", "content": prompt}])
