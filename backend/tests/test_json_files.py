from pathlib import Path

import pytest

from app.storage import json_files
from app.storage.json_files import read_json, write_json


def test_read_json_accepts_utf8_bom(tmp_path) -> None:
    path = tmp_path / "artifact.json"
    path.write_bytes(b'\xef\xbb\xbf{"schema_version": 1, "ok": true}')

    assert read_json(path) == {"schema_version": 1, "ok": True}


def test_write_json_does_not_write_utf8_bom(tmp_path) -> None:
    path = tmp_path / "artifact.json"

    write_json(path, {"schema_version": 1})

    assert not path.read_bytes().startswith(b"\xef\xbb\xbf")


def test_read_json_retries_transient_windows_sharing_error(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "artifact.json"
    path.write_text('{"ok": true}', encoding="utf-8")
    original_read_text = Path.read_text
    attempts = 0
    delays: list[float] = []

    def read_text(target: Path, *args, **kwargs) -> str:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise PermissionError("transient Windows sharing conflict")
        return original_read_text(target, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(json_files.time, "sleep", delays.append)

    assert read_json(path) == {"ok": True}
    assert attempts == 3
    assert delays == list(
        json_files.ATOMIC_REPLACE_RETRY_DELAYS_SECONDS[:2]
    )
