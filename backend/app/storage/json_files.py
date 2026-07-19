import json
import time
from pathlib import Path
from typing import Any

from app.storage.atomic_files import (
    ATOMIC_REPLACE_RETRY_DELAYS_SECONDS,
    atomic_replace,
)


def read_json(path: Path, default: Any = None) -> Any:
    for attempt in range(len(ATOMIC_REPLACE_RETRY_DELAYS_SECONDS) + 1):
        try:
            if not path.exists():
                return default
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except PermissionError:
            if attempt == len(ATOMIC_REPLACE_RETRY_DELAYS_SECONDS):
                raise
            time.sleep(ATOMIC_REPLACE_RETRY_DELAYS_SECONDS[attempt])
    raise AssertionError("JSON read retry loop exited unexpectedly.")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    atomic_replace(tmp_path, path)
