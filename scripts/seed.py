"""Reset SQLite, build model v1..v3, ingest every document into HydraDB.

v4 is deliberately not seeded: it is saved live during the demo.
Usage: uv run python scripts/seed.py [--skip-hydra]
"""
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import store, versioning  # noqa: E402
from app.config import MODELS_DIR  # noqa: E402
from app.seed_content import (  # noqa: E402
    DEALS, DOC_REFERENCES, DOCUMENTS, MEMBERSHIPS, SEEDED_VERSIONS, USERS,
)


def main(skip_hydra: bool = False) -> None:
    store.init_db(reset=True)
    if MODELS_DIR.exists():
        shutil.rmtree(MODELS_DIR)
    store.executemany("INSERT INTO users(email, name, title, role) VALUES (:email,:name,:title,:role)", USERS)
    store.executemany(
        "INSERT INTO deals(id, codename, company, sector, status, summary) "
        "VALUES (:id,:codename,:company,:sector,:status,:summary)", DEALS)
    store.executemany("INSERT INTO deal_members(deal_id, email) VALUES (?,?)", MEMBERSHIPS)
    store.executemany(
        "INSERT INTO doc_references(doc_id, doc_title, deal_id, metric, quoted_value, quoted_version, owner_email) "
        "VALUES (?,?,?,?,?,?,?)", DOC_REFERENCES)

    if skip_hydra:
        versioning.ingest_documents = lambda docs, wait=True, **_: [versioning.record_document(d) for d in docs]

    for version in SEEDED_VERSIONS:
        result = versioning.save_version("falcon", version, wait=False)
        print(f"falcon v{version}: {len(result['changes'])} cell changes, irr={result['metrics']['irr']}")

    if skip_hydra:
        for d in DOCUMENTS:
            versioning.record_document(d)
        print(f"recorded {len(DOCUMENTS)} documents in SQLite (HydraDB skipped)")
        return

    ids = versioning.ingest_documents(DOCUMENTS, wait=False)
    changelog_ids = [f"falcon_model_changelog_v{v}" for v in SEEDED_VERSIONS]
    print(f"ingested {len(ids)} documents + {len(changelog_ids)} change logs; waiting for indexing...")
    versioning.wait_until_searchable(ids + changelog_ids, timeout=600)
    print("all documents searchable")
    print("stale references:", len(store.stale_references("falcon")))


if __name__ == "__main__":
    main(skip_hydra="--skip-hydra" in sys.argv)
