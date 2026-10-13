"""Issue, list and revoke API tokens (used by MCP clients such as Tessel).

    uv run python scripts/api_token.py create priya@northbeam.com "Tessel on Priya's laptop"
    uv run python scripts/api_token.py list priya@northbeam.com
    uv run python scripts/api_token.py revoke priya@northbeam.com 1a2b3c4d
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import store, tokens  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] not in ("create", "list", "revoke"):
        print(__doc__.strip())
        return 2
    store.init_db()
    command, email = argv[0], argv[1]
    if command == "create":
        try:
            token = tokens.create(email, " ".join(argv[2:]))
        except ValueError as e:
            print(e)
            return 1
        print(f"Token for {email} (shown once, keep it secret):\n{token}")
        return 0
    if command == "list":
        for t in tokens.list_for(email):
            print(f"{t['id']}  created {t['created_at']}  last used {t['last_used_at'] or 'never'}  {t['label']}")
        return 0
    if len(argv) < 3:
        print("revoke needs the token id shown by list")
        return 2
    if not tokens.revoke(email, argv[2]):
        print("No such token")
        return 1
    print("Revoked")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
