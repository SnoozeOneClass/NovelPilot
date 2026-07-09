from pathlib import Path
from threading import Lock


_ACTIVE_RUNNERS: set[str] = set()
_ACTIVE_RUNNERS_LOCK = Lock()


def begin_active_runner(project_path: Path) -> bool:
    key = _runner_key(project_path)
    with _ACTIVE_RUNNERS_LOCK:
        if key in _ACTIVE_RUNNERS:
            return False
        _ACTIVE_RUNNERS.add(key)
        return True


def end_active_runner(project_path: Path) -> None:
    key = _runner_key(project_path)
    with _ACTIVE_RUNNERS_LOCK:
        _ACTIVE_RUNNERS.discard(key)


def has_active_runner(project_path: Path) -> bool:
    key = _runner_key(project_path)
    with _ACTIVE_RUNNERS_LOCK:
        return key in _ACTIVE_RUNNERS


def _runner_key(project_path: Path) -> str:
    return str(project_path.resolve())
