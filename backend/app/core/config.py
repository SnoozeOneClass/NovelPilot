from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[3]
CONFIG_DIR = ROOT_DIR / "config"
OUTPUT_DIR = ROOT_DIR / "output"
ACTIVE_PROJECT_PATH = CONFIG_DIR / "active-project.local.json"
LLM_PROFILES_PATH = CONFIG_DIR / "llm-profiles.local.json"


def ensure_runtime_dirs() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

