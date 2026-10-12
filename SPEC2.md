# Provenance v2 -- real ingestion + git-style model history

Replaces the scripted v1/v2/v3 model with (1) importing a real folder of files and (2) a commit graph
per financial model. SPEC.md conventions still hold (users table, ACL principals, HydraDB collection
`brain`, SQLite as the registry of which brain docs exist, LLM = OpenAI via `app.config.LLM_MODEL`
using the Responses API). Do NOT read or touch ../litecel.

## Concepts

- **Model**: one financial model lineage inside a deal (e.g. "Falcon LBO model"). Files are grouped
  into a model when their structure matches (sheet names + row labels Jaccard >= 0.5) within a deal.
- **Commit**: one observed state of a workbook. `id` = first 10 hex of sha256 over the normalized
  cell map (sheet!cell -> formula or value). Identical content = same commit (dedupe).
- **Branch**: one file path within a model. Branch name = file stem with the shared model stem
  removed and cleaned, e.g. "Falcon Model v3 - downside case (PS).xlsx" -> "downside-case-ps";
  the oldest file in the model is branch "main". A file re-saved in place adds commits to its branch.
- **Parent**: re-save of the same path -> previous head of that branch. A new file in an existing
  model -> the most similar existing commit (by fraction of identical cells) observed before it (fork).
  Commits are processed in file mtime order.
- **Commit message**: one line, git style, written by the LLM from the cell diff vs parent
  (e.g. "Cut revenue growth 12% -> 8%; FY26 EBITDA $9.68M -> $9.33M"). Fallback without LLM:
  top 3 changed labels.
- **Key metrics**: per model, the LLM maps metric names (revenue_growth, ebitda_margin, fy26_revenue,
  fy26_ebitda, exit_multiple, irr, moic -- only those present) to cells ONCE from a dump of labels and
  values; stored in `model_metric_cells`; extraction per commit is then deterministic.
- **Tags**: a document that quotes model numbers pins the commit it was based on (the latest commit
  before the document date whose metric values match the quotes best). Shown as tags on the graph.

## SQLite (new tables; drop model_versions/cell_changes/key_metrics; keep users/deals/deal_members/documents)

```
models(id PK, deal_id, name, created_at)
commits(id PK, model_id, branch, parent_id NULL, author_email, committed_at, message, file_path, file_mtime)
branches(model_id, name, head_commit, file_path, PRIMARY KEY(model_id, name))
cell_changes(commit_id, sheet, cell, label, old_value, new_value, old_formula, new_formula)
model_metric_cells(model_id, metric, sheet, cell)
commit_metrics(commit_id, metric, value)
doc_references(doc_id, doc_title, deal_id, model_id, metric, quoted_value, quoted_commit, owner_email)
files_seen(path PK, sha256, mtime, kind, doc_id NULL, commit_id NULL)   -- incremental rescans
```

## Modules and ownership

| Module | Owner | API |
|---|---|---|
| `app/workbook.py` | vcs agent | `read_workbook(path) -> {sheets:{name:{cell:{"value","formula","label"}}}}` (reads both data_only and formulas; label = nearest text cell to the left in the row, plus the column header above if found, e.g. "FY26 Revenue ($M)"); `cell_map_hash(wb) -> str`; `diff(wb_a, wb_b) -> list[change]` |
| `app/vcs.py` | vcs agent | `record_snapshot(path, deal_id, author_email, saved_at) -> dict | None` (None if unchanged / duplicate; else the commit dict incl. `changes`, `metrics`, `prior_metrics`, `model`, `branch`, `parent_id`, `is_fork`); `graph(model_id) -> {model, commits:[...], branches:[...], tags:[...]}`; `list_models(deal_id)`; `commit_diff(a, b) -> list[change]` (any two commits, not only parent/child); `get_commit(id)`; `latest_metrics(model_id)`; `stale_references(deal_id)` |
| `app/ingest.py` | ingest agent | `scan(root, wait=False) -> {commits:[...], docs:[...], skipped}`; resolves deal from first folder level under root (`_firm` = firm-wide); creates deal + members from optional `deal.json` {codename, company, sector, status, summary, team:[emails]}; author from optional `_authors.json` {filename: email} else deal team lead; Excel -> `vcs.record_snapshot`, then ingests a commit change-log doc + (first time per model) a workbook summary doc into HydraDB; PDF (pypdf) / DOCX (python-docx) / TXT / MD / EML -> text -> HydraDB app_knowledge with ACL; LLM extracts quoted metrics from memo/deck-like docs -> doc_references; registers every doc in SQLite `documents` |
| `app/watcher.py` | ingest agent | background thread polling the import root every 3s via `ingest.scan`; pushes events to an in-memory queue: `events_since(seq) -> [{seq, type: "commit"|"doc", ...}]` |
| `scripts/make_samples.py` | ingest agent | writes a realistic messy folder `samples/Northbeam Deals/` (see below) |
| `scripts/seed.py` | ingest agent | rewrite: init_db(reset), users only, delete stale HydraDB ids from previous runs if cheap, then `ingest.scan(samples root)` |
| `static/index.html` | ui agent | git-graph UI |
| `app/main.py`, `app/agent.py`, `app/brain.py` | main | endpoints + tools below |

## Sample corpus (samples/Northbeam Deals/)

Reuse the fictional world and document texts from `app/seed_content.py` (keep it as the content source).
- `Falcon/deal.json`, `Falcon/_authors.json`
- `Falcon/Models/`: "Falcon LBO v1.xlsx" (Sep 20), "Falcon LBO v2.xlsx" (Oct 2), "Falcon LBO v2 - downside case (PS).xlsx"
  (Oct 4, fork of v2: growth 6%, margin 16%), "Falcon LBO v3.xlsx" (Oct 8), "Falcon LBO v3 (DO comments).xlsx"
  (Oct 9, minor edits: added a note cell + capex tweak). Set file mtimes to those dates (os.utime).
  Realistic layout: sheets "Cover", "Assumptions", "Operating Model" (years as columns FY25..FY30, labels
  in column B, headers in row 4), "Returns". Inputs as values; outputs written as values (openpyxl cannot
  compute formulas) and also store the formula text in a cell comment-free way is NOT needed.
- `Falcon/Memos/` IC memo draft (.docx), `Falcon/Decks/` IC deck outline (.docx or .md), `Falcon/Calls/` transcripts (.txt),
  `Falcon/Email/` (.eml), `Falcon/Notes/` Slack export (.md), QoE note (.pdf via a minimal PDF writer or reportlab if installed; else .txt).
- `Harbor/` same pattern, one model with 2 versions.
- `Kestrel/` pass memo (walled).
- `_firm/` policies, IC process, expertise directory, anonymised precedent.
- `samples/_live/Falcon LBO v4.xlsx`: prepared next save (exit multiple 11.0x -> 9.5x), for the demo button
  that copies it into Falcon/Models/ (simulating a save); the real demo can also edit+save in Excel.

## HTTP API changes (main)

```
GET  /api/deals/{deal}/models?user=                       -> {models:[{id,name,branches,commit_count,head}]}
GET  /api/models/{model_id}/graph?user=                   -> vcs.graph(...)   403 if walled
GET  /api/commits/diff?user=&a=&b=                        -> {changes:[...]}
GET  /api/deals/{deal}/stale?user=                        -> {stale:[...]}  (now commit-based)
POST /api/import {user, path?}                            -> scan summary (default path: samples root)
POST /api/demo/save {user, deal}                          -> copies samples/_live next file in; watcher picks it up
GET  /api/events?user=&since=                             -> {events:[...], seq}  (commit events carry the agent alert once ready; filtered by access)
```
Agent tools become: search_brain, list_my_deals, list_models(deal_id), get_model_graph(model_id),
diff_commits(a, b), find_stale_references(deal_id).
