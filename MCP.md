# Using the brain from another app (MCP)

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
- No LLM runs on this path: the client's model reads the results. Retrieval
  still goes to HydraDB, so the privacy note in Tessel's AI_ROADMAP.md (R3,
  private retrieval backend) applies until a local backend exists.
- Browser requests must come from localhost or an origin listed in
  `MCP_ALLOWED_ORIGINS` (comma separated).
