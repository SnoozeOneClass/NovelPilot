from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

from app.core.config import DATA_DIR

AUTHORING_DATABASE_ENV = "NOVELPILOT_AUTHORING_DATABASE_URL"
DEFAULT_AUTHORING_DATABASE_PATH = DATA_DIR / "authoring.sqlite3"
DEFAULT_AUTHORING_MODEL_METADATA_PATH = (
    DATA_DIR.parent / "config" / "authoring-model-metadata.local.json"
)


def authoring_database_path(value: str | None = None) -> Path:
    """Resolve only the independent authoring SQLite database location."""
    configured = value or os.getenv(AUTHORING_DATABASE_ENV)
    if not configured:
        return DEFAULT_AUTHORING_DATABASE_PATH
    if "://" not in configured:
        return Path(configured).expanduser().resolve()
    parsed = urlsplit(configured)
    if parsed.scheme not in {"sqlite", "sqlite+aiosqlite"}:
        raise ValueError("the authoring baseline supports only an isolated SQLite database")
    if parsed.netloc not in {"", "localhost"}:
        raise ValueError("SQLite authoring database URL cannot name a remote host")
    raw_path = unquote(parsed.path)
    if os.name == "nt" and raw_path.startswith("/") and len(raw_path) > 2:
        raw_path = raw_path[1:]
    return Path(raw_path).expanduser().resolve()
