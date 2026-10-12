import pytest

from app import config, store, vcs
from app.seed_content import DEALS, MEMBERSHIPS, USERS


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Every test gets its own SQLite file and snapshot dir; nothing touches data/brain.db or the network."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "brain.db")
    monkeypatch.setattr(vcs, "llm_metric_cells", lambda wb: {})
    monkeypatch.setattr(vcs, "llm_commit_message", lambda *a, **k: None)
    store.init_db(reset=True)
    store.executemany("INSERT INTO users(email, name, title, role) VALUES (:email, :name, :title, :role)", USERS)
    store.executemany(
        "INSERT INTO deals(id, codename, company, sector, status, summary) "
        "VALUES (:id, :codename, :company, :sector, :status, :summary)",
        DEALS,
    )
    store.executemany("INSERT INTO deal_members(deal_id, email) VALUES (?, ?)", MEMBERSHIPS)
    return tmp_path
