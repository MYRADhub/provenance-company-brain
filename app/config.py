import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "brain.db"
MODELS_DIR = DATA_DIR / "models"
STATIC_DIR = ROOT / "static"

# The firm this brain serves. The defaults are the demo's fictional fund; a real firm sets its own.
FIRM_NAME = os.environ.get("BRAIN_FIRM_NAME", "Northbeam Capital")
FIRM_DESCRIPTION = os.environ.get("BRAIN_FIRM_DESCRIPTION", "a mid-market private equity fund")
# Email domain of the firm's people: firm-wide documents are readable by anyone at it.
FIRM_DOMAIN = os.environ.get("BRAIN_FIRM_DOMAIN", "northbeam.com")
# Prefix of deal-team principals (group:<slug>:<deal>). Changing it needs scripts/reindex.py.
FIRM_SLUG = os.environ.get("BRAIN_FIRM_SLUG", "northbeam")
# The shared drive the brain watches: one folder per deal, plus _firm for firm-wide documents.
BUNDLED_SAMPLES = ROOT / "samples" / "Northbeam Deals"
IMPORT_ROOT = Path(os.environ["BRAIN_IMPORT_ROOT"]).expanduser() if os.environ.get("BRAIN_IMPORT_ROOT") else BUNDLED_SAMPLES


def is_demo() -> bool:
    """Running on the bundled sample corpus, whose story is set on one fixed day."""
    return IMPORT_ROOT.resolve() == BUNDLED_SAMPLES.resolve()


def deal_group(deal_id: str) -> str:
    return f"group:{FIRM_SLUG}:{deal_id}"


HYDRA_DATABASE = os.environ.get("HYDRA_DB_DATABASE", "northbeam_capital")
COLLECTION = "brain"
LLM_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.5")


@lru_cache(maxsize=1)
def hydra():
    from hydra_db import HydraDB

    return HydraDB(token=os.environ["HYDRA_DB_API_KEY"])
