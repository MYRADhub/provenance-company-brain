"""Reset the demo: rebuild the sample folder, reset SQLite, register users, import the folder.

Deals, teams, models, commits and documents all come from importing `samples/Northbeam Deals/`,
the same path a real firm's shared drive would take. HydraDB ids are stable per file path, so a
re-seed upserts rather than duplicates. Ids from the earlier hand-seeded demo are left untouched.

    uv run python scripts/seed.py            # import, do not wait for HydraDB indexing
    uv run python scripts/seed.py --wait     # also wait until everything is searchable
    uv run python scripts/seed.py --skip-hydra   # SQLite only (offline)
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import make_samples  # noqa: E402

from app import ingest, store  # noqa: E402
from app.seed_content import USERS  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wait", action="store_true", help="wait for HydraDB indexing to finish")
    parser.add_argument("--skip-hydra", action="store_true", help="do not call HydraDB")
    args = parser.parse_args()

    if args.skip_hydra:
        ingest.hydra_ingest = lambda items: None

    # Hold the import lock across the reset so a running server's watcher cannot interleave.
    with ingest.exclusive():
        make_samples.build()
        store.init_db(reset=True)
        store.executemany("INSERT INTO users(email, name, title, role) VALUES (?,?,?,?)",
                          [(u["email"], u["name"], u["title"], u["role"]) for u in USERS])
        result = ingest.scan(ingest.IMPORT_ROOT, wait=args.wait, lock=False)
    print(f"commits: {len(result['commits'])}, documents: {len(result['docs'])}, "
          f"change logs + model overviews: {len(result['changelogs'])}, skipped: {result['skipped']}")
    for c in result["commits"]:
        parent = (c.get("parent_id") or "-")[:7]
        print(f"  {c['deal_id']:8} {c['id'][:7]} <- {parent:7} [{c.get('branch')}] {c.get('message')}")
    for e in result["errors"]:
        print(f"  ERROR {e['path']}: {e['error']}")
    refs = store._rows("SELECT doc_title, metric, quoted_value, quoted_commit FROM doc_references ORDER BY doc_title")
    print(f"doc references: {len(refs)}")
    for r in refs:
        print(f"  {r['doc_title'][:40]:40} {r['metric']:15} {r['quoted_value']:<8} @ {(r['quoted_commit'] or '-')[:7]}")


if __name__ == "__main__":
    main()
