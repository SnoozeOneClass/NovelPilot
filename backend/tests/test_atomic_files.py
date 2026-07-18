from pathlib import Path

import pytest

from app.storage import atomic_files


def test_atomic_replace_retries_transient_permission_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "value.json.tmp"
    target = tmp_path / "value.json"
    source.write_text("new", encoding="utf-8")
    target.write_text("old", encoding="utf-8")
    original_replace = Path.replace
    attempts = 0
    delays: list[float] = []

    def replace(path: Path, destination: Path) -> Path:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise PermissionError("transient file sharing conflict")
        return original_replace(path, destination)

    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(atomic_files.time, "sleep", delays.append)

    atomic_files.atomic_replace(source, target)

    assert attempts == 3
    assert delays == list(atomic_files.ATOMIC_REPLACE_RETRY_DELAYS_SECONDS[:2])
    assert target.read_text(encoding="utf-8") == "new"
    assert not source.exists()


def test_atomic_replace_fails_after_bounded_permission_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "value.json.tmp"
    target = tmp_path / "value.json"
    source.write_text("new", encoding="utf-8")
    attempts = 0
    delays: list[float] = []

    def replace(_path: Path, _destination: Path) -> Path:
        nonlocal attempts
        attempts += 1
        raise PermissionError("persistent access denial")

    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr(atomic_files.time, "sleep", delays.append)

    with pytest.raises(PermissionError, match="persistent access denial"):
        atomic_files.atomic_replace(source, target)

    assert attempts == len(atomic_files.ATOMIC_REPLACE_RETRY_DELAYS_SECONDS) + 1
    assert delays == list(atomic_files.ATOMIC_REPLACE_RETRY_DELAYS_SECONDS)
    assert source.read_text(encoding="utf-8") == "new"
    assert not target.exists()
