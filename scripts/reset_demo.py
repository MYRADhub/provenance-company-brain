"""Undo live-demo saves without re-seeding.

Removes every file under the import root that mirrors a file in samples/_live, deletes the commits
those files produced (and their docs), and moves branch heads back to the surviving parent.
HydraDB copies of deleted change logs stay behind but are invisible: brain.py only returns
documents registered in SQLite.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ingest, store  # noqa: E402


def main() -> None:
    with ingest.exclusive():
        for src in sorted(p for p in ingest.LIVE_ROOT.rglob("*") if p.is_file()):
            dest = ingest.IMPORT_ROOT / src.relative_to(ingest.LIVE_ROOT)
            seen = store._rows("SELECT commit_id FROM files_seen WHERE path = ?", (str(dest),))
            seen += store._rows("SELECT commit_id FROM files_seen WHERE path = ?", (str(dest.relative_to(ingest.IMPORT_ROOT)),))
            commits = {r["commit_id"] for r in seen if r["commit_id"]}
            commits |= {r["id"] for r in store._rows("SELECT id FROM commits WHERE file_path IN (?, ?)",
                                                     (str(dest), str(dest.relative_to(ingest.IMPORT_ROOT))))}
            for cid in commits:
                row = store._rows("SELECT model_id, branch, parent_id FROM commits WHERE id = ?", (cid,))
                if not row:
                    continue
                model_id, branch, parent = row[0]["model_id"], row[0]["branch"], row[0]["parent_id"]
                parent_file = store._rows("SELECT file_path FROM commits WHERE id = ?", (parent,))
                if parent:
                    store.execute("UPDATE branches SET head_commit = ?, file_path = ? WHERE model_id = ? AND name = ? AND head_commit = ?",
                                  (parent, parent_file[0]["file_path"] if parent_file else None, model_id, branch, cid))
                else:
                    store.execute("DELETE FROM branches WHERE model_id = ? AND name = ? AND head_commit = ?", (model_id, branch, cid))
                for table, col in (("cell_changes", "commit_id"), ("commit_metrics", "commit_id"), ("commits", "id")):
                    store.execute(f"DELETE FROM {table} WHERE {col} = ?", (cid,))
                store.execute("DELETE FROM documents WHERE doc_id = ?", (f"f_commit_{cid}",))
                print(f"removed commit {cid[:7]} ({branch})")
            store.execute("DELETE FROM files_seen WHERE path IN (?, ?)", (str(dest), str(dest.relative_to(ingest.IMPORT_ROOT))))
            if dest.exists():
                dest.unlink()
                print(f"removed {dest.relative_to(ingest.IMPORT_ROOT)}")


if __name__ == "__main__":
    main()
