from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from app.authoring.models import AuthoringMetadataDocument, AuthoringModelMetadata
from app.authoring.models.catalog import ProfilesDocument, StoredProfile, encode_profiles_document


def test_metadata_cli_upserts_current_fingerprint_without_exposing_secret(tmp_path: Path) -> None:
    secret = "must-never-appear-in-metadata-or-output"
    profile = StoredProfile(
        id="writer-a",
        display_name="Writer A",
        api_family="openai_responses",
        base_url="https://provider.invalid/v1",
        api_key=secret,
        model_id="opaque-writer",
        request_options={"max_tokens": 2048},
    )
    profiles_path = tmp_path / "profiles.json"
    profiles_path.write_bytes(encode_profiles_document(ProfilesDocument(profiles=[profile])))
    metadata_path = tmp_path / "authoring-metadata.json"
    unrelated = AuthoringModelMetadata(
        profile_id="other",
        configuration_fingerprint="1" * 64,
        context_window=4096,
        max_output_tokens=512,
    )
    metadata_path.write_text(
        AuthoringMetadataDocument(profiles=[unrelated]).model_dump_json(),
        encoding="utf-8",
    )
    script = Path(__file__).parents[3] / "scripts" / "upsert_authoring_metadata.py"

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "writer-a",
            "--profiles",
            str(profiles_path),
            "--output",
            str(metadata_path),
            "--context-window",
            "32768",
            "--max-output-tokens",
            "2048",
            "--input-price",
            "1.25",
            "--output-price",
            "5",
            "--cache-price",
            "0.5",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    document = AuthoringMetadataDocument.model_validate_json(metadata_path.read_bytes())
    assert [item.profile_id for item in document.profiles] == ["other", "writer-a"]
    saved = document.profiles[1]
    assert saved.configuration_fingerprint == profile.configuration_fingerprint
    assert saved.input_price_per_million == 1.25
    assert secret not in result.stdout + result.stderr + metadata_path.read_text("utf-8")

    updated = subprocess.run(
        [
            sys.executable,
            str(script),
            "writer-a",
            "--profiles",
            str(profiles_path),
            "--output",
            str(metadata_path),
            "--context-window",
            "65536",
            "--max-output-tokens",
            "2048",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    refreshed = AuthoringMetadataDocument.model_validate_json(metadata_path.read_bytes())
    assert updated.returncode == 0
    assert [item.profile_id for item in refreshed.profiles] == ["other", "writer-a"]
    assert refreshed.profiles[1].context_window == 65_536
    assert refreshed.profiles[1].metadata_version == 2

    before_idempotent = metadata_path.read_bytes()
    repeated = subprocess.run(
        [
            sys.executable,
            str(script),
            "writer-a",
            "--profiles",
            str(profiles_path),
            "--output",
            str(metadata_path),
            "--context-window",
            "65536",
            "--max-output-tokens",
            "2048",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert repeated.returncode == 0
    assert metadata_path.read_bytes() == before_idempotent
    assert not list(tmp_path.glob(f".{metadata_path.name}.*.tmp"))


def test_metadata_cli_rejects_mismatched_output_limit_without_changing_file(
    tmp_path: Path,
) -> None:
    profile = StoredProfile(
        id="writer-a",
        display_name="Writer A",
        api_family="openai_responses",
        base_url="https://provider.invalid/v1",
        api_key="local-secret",
        model_id="opaque-writer",
        request_options={"max_tokens": 2048},
    )
    profiles_path = tmp_path / "profiles.json"
    profiles_path.write_bytes(encode_profiles_document(ProfilesDocument(profiles=[profile])))
    metadata_path = tmp_path / "authoring-metadata.json"
    metadata_path.write_text(
        AuthoringMetadataDocument(profiles=[]).model_dump_json() + "\n", encoding="utf-8"
    )
    before = metadata_path.read_bytes()
    script = Path(__file__).parents[3] / "scripts" / "upsert_authoring_metadata.py"

    mismatch = subprocess.run(
        [
            sys.executable,
            str(script),
            "writer-a",
            "--profiles",
            str(profiles_path),
            "--output",
            str(metadata_path),
            "--context-window",
            "8192",
            "--max-output-tokens",
            "1024",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    impossible = subprocess.run(
        [
            sys.executable,
            str(script),
            "writer-a",
            "--profiles",
            str(profiles_path),
            "--output",
            str(metadata_path),
            "--context-window",
            "2048",
            "--max-output-tokens",
            "2048",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    negative_price = subprocess.run(
        [
            sys.executable,
            str(script),
            "writer-a",
            "--profiles",
            str(profiles_path),
            "--output",
            str(metadata_path),
            "--context-window",
            "8192",
            "--max-output-tokens",
            "2048",
            "--input-price",
            "-1",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert mismatch.returncode == impossible.returncode == negative_price.returncode == 2
    assert "must equal" in mismatch.stderr
    assert "smaller than context_window" in impossible.stderr
    assert "greater than or equal to 0" in negative_price.stderr
    assert metadata_path.read_bytes() == before
    assert not list(tmp_path.glob(f".{metadata_path.name}.*.tmp"))
