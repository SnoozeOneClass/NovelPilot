from __future__ import annotations

import json
from pathlib import Path

from app.profiles import ProfilesDocument
from app.security import audit as audit_module
from app.security.audit import audit_runtime_paths


def _profiles(secret: str = "local-provider-secret") -> ProfilesDocument:
    return ProfilesDocument.model_validate(
        {
            "schema_version": 2,
            "selected_profile_id": "grok-4.5",
            "profiles": [
                {
                    "id": "grok-4.5",
                    "display_name": "Grok 4.5",
                    "api_family": "openai_responses",
                    "base_url": "https://provider.example/v1",
                    "api_key": secret,
                    "model_id": "grok-4.5",
                    "request_options": {},
                    "enabled": True,
                    "capability_test": None,
                }
            ],
        }
    )


def test_audit_scans_database_backup_export_and_report_without_exposing_secret(
    tmp_path: Path,
) -> None:
    secret = "local-provider-secret"
    data = tmp_path / "data"
    output = tmp_path / "output"
    (data / "backups").mkdir(parents=True)
    (data / "live-observations").mkdir(parents=True)
    output.mkdir()
    (data / "novelpilot.sqlite3").write_bytes(b"sqlite-prefix\x00" + secret.encode())
    (data / "backups" / "snapshot.sqlite3").write_bytes(secret.encode())
    (data / "live-observations" / "slot.json").write_text(secret, encoding="utf-8")
    (output / "manuscript.md").write_text(secret, encoding="utf-8")

    result = audit_runtime_paths(roots=[data, output], profiles=_profiles(secret))
    payload = json.dumps(result.to_dict(), ensure_ascii=False)

    assert result.status == "failed"
    assert result.scanned_file_count == 4
    assert {finding.path for finding in result.findings} == {
        "data/novelpilot.sqlite3",
        "data/backups/snapshot.sqlite3",
        "data/live-observations/slot.json",
        "output/manuscript.md",
    }
    assert secret not in payload


def test_audit_detects_secret_across_streaming_chunk_boundary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    secret = "boundary-secret"
    monkeypatch.setattr(audit_module, "SCAN_CHUNK_SIZE", 8)
    data = tmp_path / "data"
    data.mkdir()
    (data / "database.sqlite3").write_bytes(b"1234567" + secret.encode("utf-8"))

    result = audit_runtime_paths(roots=[data], profiles=_profiles(secret))

    assert result.status == "failed"
    assert [finding.path for finding in result.findings] == ["data/database.sqlite3"]


def test_audit_passes_clean_runtime_paths_and_does_not_treat_base_url_as_secret(
    tmp_path: Path,
) -> None:
    data = tmp_path / "data"
    output = tmp_path / "output"
    data.mkdir()
    output.mkdir()
    (data / "novelpilot.sqlite3").write_text(
        "secret-free profile snapshot: https://provider.example/v1",
        encoding="utf-8",
    )
    (output / "manuscript.md").write_text("已提交正文", encoding="utf-8")

    result = audit_runtime_paths(roots=[data, output], profiles=_profiles())

    assert result.status == "passed"
    assert result.findings == ()
    assert result.scanned_file_count == 2


def test_audit_redacts_a_secret_embedded_in_a_path(tmp_path: Path) -> None:
    secret = "path-secret"
    data = tmp_path / "data"
    leaked = data / secret / "report.json"
    leaked.parent.mkdir(parents=True)
    leaked.write_text(secret, encoding="utf-8")

    result = audit_runtime_paths(roots=[data], profiles=_profiles(secret))
    payload = json.dumps(result.to_dict(), ensure_ascii=False)

    assert result.findings[0].path == "data/[redacted]/report.json"
    assert secret not in payload
