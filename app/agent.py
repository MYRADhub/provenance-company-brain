"""LLM agent over two sources: SQLite facts (what changed) and the HydraDB brain (why)."""

import json

from openai import OpenAI

from app import brain, governance, store, vcs
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
        "name": "list_models",
        "description": "List the financial models (Excel lineages) in a deal, with their branches, commit counts and head commits.",
        "input_schema": {
            "type": "object",
            "properties": {"deal_id": {"type": "string"}},
            "required": ["deal_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_model_graph",
        "description": (
            "Structured facts from the database: the git-style commit graph of one model. Every commit has a short hash, "
            "parent, branch (one per file), author, timestamp, one-line message, key metrics (revenue growth, EBITDA margin, "
            "FY26 revenue and EBITDA, exit multiple, IRR, MOIC) and tags (documents that quoted that commit)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"model_id": {"type": "string"}},
            "required": ["model_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "diff_commits",
        "description": (
            "Cell-level diff between any two commits of the same model (sheet, cell, label, old and new value and formula). "
            "Tells you exactly what changed, but not why; use search_brain for the why."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"from_commit": {"type": "string"}, "to_commit": {"type": "string"}},
            "required": ["from_commit", "to_commit"],
            "additionalProperties": False,
        },
    },
    {
        "name": "find_stale_references",
        "description": (
            "Find documents (IC memos, decks) that quote a model metric whose value has since changed on the model's main "
            "branch. Returns quoted value, the commit it was based on, latest value and the document owner."
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
- Database tools (list_my_deals, list_models, get_model_graph, diff_commits, find_stale_references) return hard facts: the git-style history of every Excel model -- what changed, in which commit, on which branch, when, by whom.
- search_brain returns unstructured knowledge: the reasons, context, decisions and people behind those facts.
Strong answers join them: take the fact from the database, then find the reason in the brain and cite it.

Rules:
- Ground every claim in tool results. Cite sources inline by document title in italics, e.g. *Management call - Acme Freight CFO (Oct 6)*.
- When numbers changed, say old -> new, who changed it and when (e.g. "Priya, Oct 8"), then the reason and its source. Do not show commit hashes, branch slugs, cell references or tool names unless the user asks for them.
- Information barriers are enforced. If a tool returns "restricted" or a search finds nothing relevant, do not guess or hint at what might exist.
  Say that you have nothing the user is cleared to see on that topic and that, if it relates to a walled deal,
  they should contact {compliance.get('name', 'Compliance')} ({compliance.get('email', '')}). Never confirm or deny that a restricted deal exists.
- Write for a busy partner who is not technical: lead with a one-sentence answer, then at most 4 short bullets of evidence. No headings for short answers, no tables, bold at most two key numbers. Prefer plain words over jargon. Refer to the current user as "you", never by name.
- When you notice something the user should act on (stale numbers in a deck, a contradiction with a memo, someone to loop in), end with one line starting with **Next step:**."""


def _restricted() -> dict:
    c = store.compliance_contact() or {}
    return {
        "error": "restricted",
        "message": f"No accessible records. If this relates to a walled deal, contact {c.get('name')} ({c.get('email')}).",
    }


def _model_accessible(email: str, model_id: str) -> bool:
    model = next((m for d in store.accessible_deals(email) for m in vcs.list_models(d["id"]) if m["id"] == model_id), None)
    return model is not None


def _commit_accessible(email: str, commit_id: str) -> bool:
    commit = vcs.get_commit(commit_id) if commit_id else None
    return bool(commit) and _model_accessible(email, commit["model_id"])


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

    if name in ("list_models", "find_stale_references"):
        deal_id = args.get("deal_id", "")
        if not store.can_access_deal(email, deal_id):
            return _restricted(), "restricted"
        if name == "list_models":
            models = vcs.list_models(deal_id)
            return {"models": models}, f"{len(models)} models"
        stale = vcs.stale_references(deal_id)
        return {"stale": stale}, f"{len(stale)} stale references"

    if name == "get_model_graph":
        if not _model_accessible(email, args.get("model_id", "")):
            return _restricted(), "restricted"
        graph = vcs.graph(args["model_id"])
        branches = len(graph.get("branches", []))
        return graph, f"{len(graph.get('commits', []))} commits on {branches} branches"

    if name == "diff_commits":
        a, b = args.get("from_commit", ""), args.get("to_commit", "")
        if not (_commit_accessible(email, a) and _commit_accessible(email, b)):
            return _restricted(), "restricted"
        changes = vcs.commit_diff(a, b)
        return {"changes": changes}, f"{len(changes)} cell changes {a[:7]} -> {b[:7]}"

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
    question = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
    governance.log(email, "question", question)
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
            if summary == "restricted":
                governance.log(email, "blocked", f"{call.name} {json.dumps(args)}", "blocked")
            items.append({"type": "function_call_output", "call_id": call.call_id, "output": json.dumps(output, default=str)})
    else:
        answer = answer or "I ran out of steps before finishing. Try a narrower question."

    cited = [s for s in sources.values() if s["title"] and s["title"] in answer]
    if not cited:
        cited = list(sources.values())[:4]
    return {"answer": answer, "tool_trace": trace, "citations": cited}


def proactive_alert(email: str, commit: dict) -> dict:
    """After a new commit lands (a model file was saved), have the agent assess its impact for this user."""
    changes = "\n".join(
        f"- {c['sheet']}!{c['cell']} {c.get('label') or ''}: {c.get('old_value')} -> {c.get('new_value')}"
        for c in (commit.get("changes") or [])[:40]
    )
    model = commit.get("model") or {}
    prompt = f"""[Automated trigger] A model file was just saved. New commit {commit['id']} on branch '{commit.get('branch')}' of
model '{model.get('name', commit.get('model_id'))}' (model_id {commit.get('model_id')}, deal {model.get('deal_id', '')}), parent {commit.get('parent_id')},
by {commit.get('author_email')} at {commit.get('committed_at')}. Commit message: {commit.get('message')}
Cell changes versus the parent:
{changes or '- none detected'}

Write a short proactive alert for me, as a notification a partner reads on their phone:
- First line: one sentence on what changed and what it does to returns.
- Then at most 3 bullets: why it changed (with source, or "no reason documented yet"), which documents now use old numbers and who owns them, and any conflict with the IC memo or firm policy.
- End with one **Next step:** line.
No headings, no commit hashes, no cell references."""
    return run(email, [{"role": "user", "content": prompt}])
