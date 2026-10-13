# Using the brain from another app (MCP and the app API)

The brain's six tools are served over the Model Context Protocol at `POST /mcp`
(Streamable HTTP, stateless, JSON responses). Any MCP client can use them;
Tessel is the first.

| Tool | Gives |
|---|---|
| `search_brain` | Passages from memos, calls, emails and change logs: the why |
| `list_my_deals` | Deals the caller is staffed on |
| `list_models` | Excel model lineages in a deal |
| `get_model_graph` | Commit graph of a model with key metrics |
| `diff_commits` | Cell-level diff between two commits |
| `find_stale_references` | Documents quoting a metric that has since changed |

## Connect

1. Issue a token for the user (shown once; only its hash is stored):

   ```
   uv run python scripts/api_token.py create priya@northbeam.com "Tessel on Priya's laptop"
   ```

2. In Tessel: Extensions > MCP servers > Add. URL `http://localhost:8000/mcp`,
   header `Authorization: Bearer pb_...`.

`scripts/api_token.py list <email>` and `revoke <email> <id>` manage tokens.

## Rules

- The token decides who the caller is. Arguments cannot name another user.
- Ethical walls apply exactly as in the brain's own assistant; a walled deal
  answers the same as one that does not exist.
- Every call is written to the governance audit log (`mcp_tool`, or `blocked`).
- No LLM runs on this path: the client's model reads the results.
- Document search runs where `BRAIN_RETRIEVAL` says: `local` (a SQLite
  full-text index in `brain.db`; nothing leaves the server) or `hydra`
  (HydraDB, hosted). Unset, it is `hydra` when `HYDRA_DB_API_KEY` is set and
  `local` otherwise. After switching, run `uv run python scripts/reindex.py`.
  `/api/v1/me` reports which one is in use.
- Browser requests must come from localhost or an origin listed in
  `MCP_ALLOWED_ORIGINS` (comma separated).

## Recording saves (`/api/v1`)

Apps record saves directly instead of waiting for the folder watcher. Same
bearer token.

- `GET /api/v1/me`: the token's user and the deals they are staffed on.
- `POST /api/v1/saves` with JSON `{deal, workbook_id, file_name, data, saved_at}`:
  `data` is the .xlsx/.xlsm in base64 (25 MB at most), `workbook_id` a stable id
  the app keeps inside the workbook (8 to 64 of `A-Z a-z 0-9 _ -`), `saved_at`
  milliseconds since the epoch. The file is kept under
  `data/uploads/<deal>/<workbook_id>/` and committed exactly as a scanned file
  would be, with the caller as author. The answer holds the commit (or `null`
  when the brain already had that content), the documents that now quote
  numbers the model no longer shows, and any warnings (for example when the
  change log could not be indexed for search).
- A deal the caller is not staffed on answers 404, the same as one that does
  not exist.
- Tessel's hidden `_tessel_meta` sheet is ignored when reading workbooks.
- `GET /api/v1/history?deal=&workbook_id=[&sheet=&cell=]`: the workbook's saved
  versions, newest first (its latest commit and every ancestor, at most 50),
  and with `sheet` and `cell` (like `C7`) the versions where that cell's value
  or formula changed, oldest first.

## Who is calling

- API tokens (`pb_...`) work everywhere, always.
- `BRAIN_AUTH_MODE=supabase` also accepts a Supabase Auth access token (the
  one Tessel holds when its user is signed in): checked against
  `SUPABASE_URL`'s signing keys, or `SUPABASE_JWT_SECRET` for projects on the
  legacy shared secret, with issuer and audience. `BRAIN_AUTH_MODE=local`
  accepts tokens from Tessel's dev auth server (`LOCAL_AUTH_URL`).
- Signing in proves who someone is; they must also be a user in the brain,
  and staffing still decides what they see.
- `BRAIN_REQUIRE_AUTH=1` closes the demo user switcher: the web UI asks for a
  token and keeps an HttpOnly session cookie (12 hours; set
  `BRAIN_SESSION_SECRET` so sessions survive restarts), and every `/api` and
  `/files` request may act only as the signed-in user. Attempts to act as
  someone else are refused and written to the audit log.
