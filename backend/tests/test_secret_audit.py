import json
from pathlib import Path

import pytest

from app.core import config as core_config
from app.schemas.profiles import LlmProfileUpsert
from app.storage import profiles as profile_storage
from app.storage import secret_audit
from app.storage.file_lock import exclusive_file_lock
from app.storage.secret_audit import (
    audit_output_for_profile_secrets,
    audit_path_for_profile_secrets,
)
from scripts import audit_output_secrets


def test_secret_audit_finds_profile_values_without_exposing_them(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output_dir = _isolate_runtime_paths(tmp_path, monkeypatch)
    project_path = output_dir / "Leaky Novel"
    project_path.mkdir(parents=True)
    (project_path / "events.jsonl").write_text(
        "provider echoed secret-key at https://api.example.com/v1",
        encoding="utf-8",
    )
    _create_profile()

    result = audit_output_for_profile_secrets()
    payload = json.dumps(result.to_dict(), ensure_ascii=False)
    finding_keys = {(finding.path, finding.profile_id, finding.kind) for finding in result.findings}

    assert result.status == "failed"
    assert result.profile_count == 1
    assert result.scanned_file_count == 1
    assert ("Leaky Novel/events.jsonl", "main", "api_key") in finding_keys
    assert ("Leaky Novel/events.jsonl", "main", "base_url") in finding_keys
    assert "secret-key" not in payload
    assert "https://api.example.com/v1" not in payload


def test_secret_audit_passes_when_output_only_has_sanitized_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output_dir = _isolate_runtime_paths(tmp_path, monkeypatch)
    project_path = output_dir / "Clean Novel"
    project_path.mkdir(parents=True)
    (project_path / "project.json").write_text(
        '{"active_profile_id": "main", "model_snapshot": "example-model"}',
        encoding="utf-8",
    )
    _create_profile()

    result = audit_output_for_profile_secrets()

    assert result.status == "passed"
    assert result.findings == []
    assert result.scanned_file_count == 1


def test_secret_audit_scans_active_lock_file_without_touching_locked_byte(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output_dir = _isolate_runtime_paths(tmp_path, monkeypatch)
    project_path = output_dir / "Clean Novel"
    project_path.mkdir(parents=True)
    (project_path / "project.json").write_text("{}", encoding="utf-8")
    lock_path = project_path / ".events.lock"
    lock_path.write_bytes(b"\0secret-key")
    _create_profile()

    with exclusive_file_lock(lock_path):
        result = audit_output_for_profile_secrets()

    assert result.status == "failed"
    assert result.scanned_file_count == 2
    assert any(
        finding.path == "Clean Novel/.events.lock" and finding.kind == "api_key"
        for finding in result.findings
    )


@pytest.mark.parametrize("error_type", [FileNotFoundError, PermissionError])
def test_secret_audit_retries_the_complete_scan_after_transient_file_error(
    tmp_path: Path,
    monkeypatch,
    error_type: type[OSError],
) -> None:
    output_dir = _isolate_runtime_paths(tmp_path, monkeypatch)
    project_path = output_dir / "Changing Novel"
    project_path.mkdir(parents=True)
    artifact_path = project_path / "artifact.json.tmp"
    artifact_path.write_text("secret-key", encoding="utf-8")
    _create_profile()
    original_read = secret_audit._read_audited_text
    read_attempts = 0

    def flaky_read(path: Path) -> str:
        nonlocal read_attempts
        if path == artifact_path:
            read_attempts += 1
            if read_attempts == 1:
                raise error_type("transient transaction race")
        return original_read(path)

    monkeypatch.setattr(secret_audit, "_read_audited_text", flaky_read)
    monkeypatch.setattr(secret_audit.time, "sleep", lambda _seconds: None)

    result = audit_output_for_profile_secrets()

    assert result.status == "failed"
    assert result.scanned_file_count == 1
    assert read_attempts == 2
    assert any(finding.path.endswith("artifact.json.tmp") for finding in result.findings)


def test_secret_audit_persistent_permission_error_fails_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    artifact_path = tmp_path / "unreadable.txt"
    artifact_path.write_text("clean", encoding="utf-8")
    read_attempts = 0

    def unreadable(_path: Path) -> str:
        nonlocal read_attempts
        read_attempts += 1
        raise PermissionError("persistently unreadable")

    monkeypatch.setattr(secret_audit, "_read_audited_text", unreadable)
    monkeypatch.setattr(secret_audit.time, "sleep", lambda _seconds: None)

    with pytest.raises(PermissionError, match="persistently unreadable"):
        audit_path_for_profile_secrets(tmp_path, profiles=[])

    assert read_attempts == len(secret_audit.SCAN_RETRY_DELAYS_SECONDS) + 1


def test_secret_audit_scans_transaction_and_temporary_files(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output_dir = _isolate_runtime_paths(tmp_path, monkeypatch)
    project_path = output_dir / "Transaction Novel"
    staged_path = (
        project_path
        / "book"
        / ".transactions"
        / "tx-001"
        / "staged"
        / "book"
        / "setup.json"
    )
    temporary_path = project_path / "project.json.tmp"
    staged_path.parent.mkdir(parents=True)
    staged_path.write_text("secret-key", encoding="utf-8")
    temporary_path.write_text("secret-key", encoding="utf-8")
    _create_profile()

    result = audit_output_for_profile_secrets()
    finding_paths = {finding.path for finding in result.findings if finding.kind == "api_key"}

    assert result.status == "failed"
    assert result.scanned_file_count == 2
    assert "Transaction Novel/book/.transactions/tx-001/staged/book/setup.json" in finding_paths
    assert "Transaction Novel/project.json.tmp" in finding_paths


def test_secret_audit_redacts_profile_values_from_finding_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output_dir = _isolate_runtime_paths(tmp_path, monkeypatch)
    project_path = output_dir / "secret-key"
    project_path.mkdir(parents=True)
    (project_path / "debug.txt").write_text("secret-key", encoding="utf-8")
    _create_profile()

    result = audit_output_for_profile_secrets()
    payload = json.dumps(result.to_dict(), ensure_ascii=False)

    assert result.status == "failed"
    assert result.findings[0].path == "[redacted]/debug.txt"
    assert "secret-key" not in payload


def test_secret_audit_cli_reports_findings_without_raw_values(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    output_dir = _isolate_runtime_paths(tmp_path, monkeypatch)
    project_path = output_dir / "Leaky Novel"
    project_path.mkdir(parents=True)
    (project_path / "debug.txt").write_text(
        "secret-key\nhttps://api.example.com/v1",
        encoding="utf-8",
    )
    _create_profile()

    exit_code = audit_output_secrets.main(["--json"])
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert exit_code == 1
    assert payload["status"] == "failed"
    assert "secret-key" not in output
    assert "https://api.example.com/v1" not in output
    assert payload["findings"][0]["path"] == "Leaky Novel/debug.txt"


def _isolate_runtime_paths(tmp_path: Path, monkeypatch) -> Path:
    config_dir = tmp_path / "config"
    output_dir = tmp_path / "output"
    monkeypatch.setattr(core_config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(core_config, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(
        profile_storage,
        "LLM_PROFILES_PATH",
        config_dir / "llm-profiles.local.json",
    )
    return output_dir


def _create_profile() -> None:
    profile_storage.upsert_profile(
        LlmProfileUpsert(
            id="main",
            name="Main Provider",
            protocol="openai-compatible",
            base_url="https://api.example.com/v1",
            api_key="secret-key",
            model="example-model",
        )
    )
