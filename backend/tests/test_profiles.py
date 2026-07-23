from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from app.agents.contracts import ProfileCapabilities
from app.profiles import (
    CapabilityEvidence,
    ProfileCatalog,
    ProfileConfigurationError,
    ProfilesDocument,
    encode_profiles_document,
    profile_configuration_fingerprint,
)


def _document(*, secret: str = "local-provider-secret") -> ProfilesDocument:
    fingerprint = profile_configuration_fingerprint(
        api_family="openai_responses",
        base_url="https://provider.example/v1",
        model_id="opaque-model",
        request_options={},
    )
    return ProfilesDocument.model_validate(
        {
            "schema_version": 2,
            "selected_profile_id": "profile-a",
            "profiles": [
                {
                    "id": "profile-a",
                    "display_name": "Profile A",
                    "api_family": "openai_responses",
                    "base_url": "https://provider.example/v1",
                    "api_key": secret,
                    "model_id": "opaque-model",
                    "request_options": {},
                    "enabled": True,
                    "capability_test": {
                        "checked_at": "2026-07-23T00:00:00+00:00",
                        "profile_fingerprint": fingerprint,
                        "source": "pydantic-ai-capability-v1",
                        "capabilities": {
                            "text_streaming": True,
                            "native_json_schema": True,
                        },
                    },
                }
            ],
        }
    )


def test_profile_document_encoding_preserves_secret_in_local_file_only() -> None:
    encoded = encode_profiles_document(_document())
    payload = json.loads(encoded)

    assert payload["profiles"][0]["api_key"] == "local-provider-secret"
    assert "********" not in encoded.decode("utf-8")


def test_committed_dual_protocol_profile_example_is_schema_valid() -> None:
    example = (
        Path(__file__).resolve().parents[2] / "config" / "llm-profiles.example.json"
    )

    document = ProfilesDocument.model_validate_json(example.read_bytes())

    assert [profile.api_family for profile in document.profiles] == [
        "openai_responses",
        "anthropic_messages",
    ]
    assert document.profiles[1].request_options["max_tokens"] == 65_536


def test_profile_probe_script_starts_without_import_drift() -> None:
    repository = Path(__file__).resolve().parents[2]

    completed = subprocess.run(
        [sys.executable, str(repository / "scripts" / "probe_profile.py"), "--help"],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "production Adapter" in completed.stdout


def test_profile_probe_cli_prints_only_sanitized_terminal_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = Path(__file__).resolve().parents[2]
    script = repository / "scripts" / "probe_profile.py"
    spec = importlib.util.spec_from_file_location("profile_probe_cli_test", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    assert isinstance(module, ModuleType)
    spec.loader.exec_module(module)
    path = tmp_path / "profiles.local.json"
    path.write_bytes(encode_profiles_document(_document()))

    async def fail_probe(*_args: object, **_kwargs: object) -> object:
        cause = RuntimeError("raw-local-provider-secret")
        error = module.ProfileCapabilityProbeError("sanitized provider timeout")
        raise error from cause

    monkeypatch.setattr(module, "probe_stored_profile", fail_probe)

    exit_code = module.main(["profile-a", "--config", str(path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Capability probe started" in captured.out
    assert captured.err.strip() == (
        "Capability probe failed: sanitized provider timeout"
    )
    assert "raw-local-provider-secret" not in captured.err


def test_record_capability_evidence_is_atomic_and_configuration_bound(
    tmp_path: Path,
) -> None:
    path = tmp_path / "profiles.local.json"
    path.write_bytes(encode_profiles_document(_document()))
    catalog = ProfileCatalog(path)
    stored = catalog.get_stored("profile-a")
    evidence = CapabilityEvidence(
        checked_at="2026-07-23T01:00:00+00:00",
        profile_fingerprint=stored.configuration_fingerprint,
        source="pydantic-ai-capability-v1",
        capabilities=ProfileCapabilities(
            text_streaming=True,
            native_json_schema=True,
        ),
    )

    catalog.record_capability_evidence(profile_id="profile-a", evidence=evidence)

    reloaded = catalog.get_stored("profile-a")
    assert reloaded.api_key.get_secret_value() == "local-provider-secret"
    assert reloaded.capability_test == evidence
    assert not (tmp_path / ".profiles.local.json.tmp").exists()


def test_stale_or_mismatched_capability_evidence_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "profiles.local.json"
    path.write_bytes(encode_profiles_document(_document()))
    catalog = ProfileCatalog(path)
    stale = CapabilityEvidence(
        checked_at="2026-07-23T01:00:00+00:00",
        profile_fingerprint="0" * 64,
        source="pydantic-ai-capability-v1",
        capabilities=ProfileCapabilities(
            text_streaming=True,
            native_json_schema=True,
        ),
    )

    with pytest.raises(ProfileConfigurationError, match="does not match"):
        catalog.record_capability_evidence(profile_id="profile-a", evidence=stale)

    changed = json.loads(path.read_bytes())
    changed["profiles"][0]["model_id"] = "changed-model"
    path.write_text(json.dumps(changed), encoding="utf-8")
    with pytest.raises(ProfileConfigurationError, match="stale"):
        catalog.resolve("profile-a")


def test_legacy_migration_evidence_never_replaces_current_adapter_probe(
    tmp_path: Path,
) -> None:
    path = tmp_path / "profiles.local.json"
    payload = json.loads(encode_profiles_document(_document()))
    payload["profiles"][0]["capability_test"]["source"] = (
        "legacy-responses-capability-v1"
    )
    path.write_text(json.dumps(payload), encoding="utf-8")
    catalog = ProfileCatalog(path)

    _selected, public = catalog.list_public()

    assert public[0].capability_status == "stale"
    with pytest.raises(ProfileConfigurationError, match="current production Adapter"):
        catalog.resolve("profile-a")
