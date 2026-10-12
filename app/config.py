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

HYDRA_DATABASE = os.environ.get("HYDRA_DB_DATABASE", "northbeam_capital")
COLLECTION = "brain"
LLM_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.5")


@lru_cache(maxsize=1)
def hydra():
    from hydra_db import HydraDB

    return HydraDB(token=os.environ["HYDRA_DB_API_KEY"])
