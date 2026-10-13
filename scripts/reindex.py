"""Index every registered document again in the configured retrieval backend.

Use after switching backends, for example to move an existing brain to the local index:

    BRAIN_RETRIEVAL=local uv run python scripts/reindex.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ingest, retrieval, store  # noqa: E402


def main() -> int:
    store.init_db()
    result = ingest.rebuild_index()
    print(f"Indexed {result['indexed']} documents in the {retrieval.backend()} backend.")
    for path in result["missing"]:
        print(f"  not found or unreadable, left out: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
