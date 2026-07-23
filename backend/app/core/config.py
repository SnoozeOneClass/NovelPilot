from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[3]
CONFIG_DIR = ROOT_DIR / "config"
DATA_DIR = ROOT_DIR / "data"
DATABASE_PATH = DATA_DIR / "novelpilot.sqlite3"
DATABASE_BACKUP_DIR = DATA_DIR / "backups"
OUTPUT_DIR = ROOT_DIR / "output"
LLM_PROFILES_PATH = CONFIG_DIR / "llm-profiles.local.json"


def ensure_runtime_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DATABASE_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

