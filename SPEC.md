# Provenance -- build contract

"The database knows what changed. The brain knows why."

Company brain for a PE fund (Northbeam Capital). SQLite holds hard facts (users, deals,
memberships, model versions, cell diffs, key metrics, doc references). HydraDB holds the
unstructured brain (memos, transcripts, emails, policies, change logs) with ACL-enforced
ethical walls. An OpenAI agent (gpt-5.5) uses tools over both.

Python 3.12, uv. Run with `uv run ...`. Env in `.env` (HYDRA_DB_API_KEY, HYDRA_DB_DATABASE=northbeam_capital,
OPENAI_API_KEY). Never print keys.

## Layout and ownership

| Path | Owner | Purpose |
|---|---|---|
| `app/config.py` | main | env loading, paths (`DATA_DIR`, `DB_PATH=data/brain.db`, `MODELS_DIR=data/models`), hydra client factory `hydra()` |
| `app/seed_content.py` | seed agent | USERS, DEALS, MEMBERSHIPS, DOCUMENTS, MODEL_VERSIONS, DOC_REFERENCES as Python data |
| `app/excel_diff.py` | seed agent | `diff_workbooks(path_a, path_b) -> list[dict]` (keys: sheet, cell, label, old_value, new_value) using openpyxl; label = text in column A of the same row |
| `app/store.py` | seed agent | SQLite schema + query helpers (API below) |
| `app/versioning.py` | seed agent | `build_workbook(deal_id, version) -> path`, `save_version(deal_id, version) -> dict` (writes xlsx, diffs vs prior, writes SQLite rows, ingests a change-log doc into HydraDB, returns {version, author, saved_at, changes, metrics}) |
| `scripts/seed.py` | seed agent | resets SQLite, builds v1..v3, ingests all docs into HydraDB, polls until searchable. v4 is NOT seeded; it is saved live in the demo |
| `app/brain.py` | main | HydraDB query wrapper (acl from store.user_principals) |
| `app/agent.py` | main | tool definitions + agent loop |
| `app/main.py` | main | FastAPI app, serves `static/` |
| `static/index.html` | ui agent | single-file UI, vanilla JS + CSS, no build step |

## SQLite schema (data/brain.db)

```
users(email PK, name, title, role)            -- role: managing_partner | principal | associate | compliance
deals(id PK, codename, company, sector, status, summary)
deal_members(deal_id, email)
documents(doc_id PK, title, deal_id NULL, kind, date, author_email, acl_json)
model_versions(deal_id, version, file_path, author_email, saved_at, note)
cell_changes(deal_id, from_version, to_version, sheet, cell, label, old_value, new_value)
key_metrics(deal_id, version, metric, value)  -- metrics: revenue_growth, ebitda_margin, fy26_revenue, fy26_ebitda, exit_multiple, irr, moic
doc_references(doc_id, doc_title, deal_id, metric, quoted_value, quoted_version, owner_email)
```

## store.py API

```
init_db(reset=False)
get_user(email) -> dict | None
list_users() -> list[dict]
user_principals(email) -> list[str]     # [email, "group:northbeam:<deal_id>" per membership]
can_access_deal(email, deal_id) -> bool
accessible_deals(email) -> list[dict]
compliance_contact() -> dict           # the compliance user
list_versions(deal_id) -> list[dict]    # each with metrics dict
get_changes(deal_id, from_v, to_v) -> list[dict]   # cell-level, spans multiple steps if needed
get_metrics(deal_id, version=None) -> dict          # latest when None
stale_references(deal_id) -> list[dict]             # refs whose quoted_value != latest metric value; include latest_value, latest_version
```

## HydraDB conventions

- database = northbeam_capital (already created, metadata schema fields: `deal`, `doc_kind`).
- one collection: `brain`.
- Every doc is `app_knowledge` with stable `id`, `title`, `type`, `timestamp`, `content.text`,
  metadata deal/doc_kind, `additional_metadata` {author, date}.
- ACL: firm-wide docs `["domain:northbeam.com"]`; deal docs `["group:northbeam:<deal_id>"]`.
- Query always sends `acl=store.user_principals(email)`.

## Fictional world

Northbeam Capital, mid-market PE. Users (all @northbeam.com):
- elena@ Elena Ruiz, Managing Partner -- member of falcon, harbor, kestrel
- david@ David Okafor, Principal -- falcon lead, also kestrel (past)
- priya@ Priya Shah, Associate -- falcon
- marcus@ Marcus Lee, Associate -- harbor only (walled from falcon)
- sam@ Sam Patel, Chief Compliance Officer -- no deal membership

Deals:
- falcon: Project Falcon = Acme Freight Systems, logistics SaaS, IC meeting Oct 15 2026, status "diligence".
- harbor: Project Harbor = Brightline Health, healthcare scheduling software, status "LOI".
- kestrel: Project Kestrel = Swiftroute, logistics SaaS, passed in Mar 2025 (customer concentration -> churn).

Falcon model versions (sheet "Assumptions" + "Returns"):
- v1 Sep 20 (david): growth 15%, margin 18%, exit 11.0x
- v2 Oct 2 (priya): growth 12% (haircut to CIM after QoE) -- IC memo draft and IC deck quote v2 numbers
- v3 Oct 8 (priya): growth 8% (after Oct 6 CFO call: two enterprise customers signalling non-renewal)
- v4 (saved live in demo, david): exit multiple 11.0x -> 9.5x (Elena's house view after Kestrel, no logistics exits above 10x)
Base FY25 revenue $48.0M. fy26_revenue = 48*(1+g); fy26_ebitda = fy26_revenue*margin; IRR/MOIC from a simple 5-year hold.

The "why" for each change lives ONLY in unstructured docs (transcript, email, memo, Slack-style
note), never in a SQLite column. Change-log docs say what changed, not why.

## HTTP API (app/main.py)

All endpoints take `user` (email) -- demo user switcher; a real deployment derives it from the session.

```
GET  /api/users                                 -> [{email,name,title,role}]
GET  /api/me?user=                              -> {user, deals:[deal...]}
POST /api/chat {user, messages:[{role,content}]} -> {answer, tool_trace:[{tool,input,summary}], citations:[{id,title,kind,deal}]}
GET  /api/deals/{deal}/versions?user=           -> {deal, versions:[{version,author_email,saved_at,note,metrics}]}   403 if walled
GET  /api/deals/{deal}/changes?user=&from=&to=  -> {changes:[...]}                                                    403 if walled
GET  /api/deals/{deal}/stale?user=              -> {stale:[...]}
POST /api/demo/save-version {user, deal}        -> {version, changes, metrics, alert:{answer, tool_trace, citations}}
```
`answer` is Markdown. 403 body: {detail: "..."}.
