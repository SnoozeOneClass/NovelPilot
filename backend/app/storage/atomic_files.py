from __future__ import annotations

import time
from pathlib import Path


ATOMIC_REPLACE_RETRY_DELAYS_SECONDS = (0.02, 0.05, 0.1, 0.2, 0.4, 0.8, 1.0)


def atomic_replace(source: Path, target: Path) -> None:
    """Replace target while tolerating bounded transient file sharing failures."""
    for attempt in range(len(ATOMIC_REPLACE_RETRY_DELAYS_SECONDS) + 1):
        try:
            source.replace(target)
            return
        except PermissionError:
            if attempt == len(ATOMIC_REPLACE_RETRY_DELAYS_SECONDS):
                raise
            time.sleep(ATOMIC_REPLACE_RETRY_DELAYS_SECONDS[attempt])
