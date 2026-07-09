import json
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api import completion as completion_api
from app.core import config as core_config
from app.core import paths as core_paths
from app.schemas.completion import LiteraryReviewRequest
from app.schemas.profiles import LlmProfileUpsert
from app.schemas.projects import CreateProjectRequest
from app.storage import profiles as profile_storage
from app.storage import projects as project_storage


def test_completion_audit_requires_active_project(tmp_path: Path, monkeypatch) -> None:
    _isolate_runtime_paths(tmp_path, monkeypatch)

    with pytest.raises(HTTPException) as exc:
        completion_api.get_completion_audit()

    assert exc.value.status_code == 404
    assert exc.value.detail == "No active project."


def test_completion_api_records_literary_review_for_active_project(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _isolate_runtime_paths(tmp_path, monkeypatch)
    project = project_storage.create_project(
        CreateProjectRequest(title="Smoke Project", operation_mode="full_auto")
    )
    project_path = Path(project.path)
    _write_smoke_artifacts(project_path)

    before = completion_api.get_completion_audit()
    record = completion_api.create_literary_review(
        LiteraryReviewRequest(
            decision="approved",
            reviewer="test reviewer",
            chapter_assessment="The chapter is coherent enough for the completion gate.",
            state_patch_assessment="The patch references the final chapter and is useful.",
            notes="Recorded through API.",
        )
    )
    after = completion_api.get_completion_audit()

    assert before.status == "pending"
    assert record.decision == "approved"
    assert (project_path / "exports" / "literary_review.json").exists()
    assert (project_path / "exports" / "literary_review.md").exists()
    assert after.status == "passed"
    assert {gate.id: gate.status for gate in after.gates} == {
        "output_secret_audit": "passed",
        "live_provider_smoke": "passed",
        "literary_quality_review": "passed",
    }


def test_completion_api_refuses_literary_review_before_live_smoke_passes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _isolate_runtime_paths(tmp_path, monkeypatch)
    project = project_storage.create_project(
        CreateProjectRequest(title="Failed Smoke Project", operation_mode="full_auto")
    )
    _write_smoke_artifacts(Path(project.path), status="failed")

    with pytest.raises(HTTPException) as exc:
        completion_api.create_literary_review(
            LiteraryReviewRequest(
                decision="approved",
                reviewer="test reviewer",
                chapter_assessment="The chapter is coherent enough for the completion gate.",
                state_patch_assessment="The patch references the final chapter and is useful.",
                notes="Recorded through API.",
            )
        )

    assert exc.value.status_code == 400
    assert "live provider smoke passes" in str(exc.value.detail)


def test_completion_api_accepts_bom_completion_evidence(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _isolate_runtime_paths(tmp_path, monkeypatch)
    project = project_storage.create_project(
        CreateProjectRequest(title="Bom Smoke Project", operation_mode="full_auto")
    )
    project_path = Path(project.path)
    _write_smoke_artifacts(project_path)
    _add_utf8_bom(project_path / "exports" / "live_smoke_report.json")

    before = completion_api.get_completion_audit()
    completion_api.create_literary_review(
        LiteraryReviewRequest(
            decision="approved",
            reviewer="test reviewer",
            chapter_assessment="The chapter is coherent enough for the completion gate.",
            state_patch_assessment="The patch references the final chapter and is useful.",
            notes="Recorded through API.",
        )
    )
    _add_utf8_bom(project_path / "exports" / "literary_review.json")
    after = completion_api.get_completion_audit()

    assert {gate.id: gate.status for gate in before.gates}["live_provider_smoke"] == "passed"
    assert after.status == "passed"


def test_completion_api_fails_when_active_project_contains_profile_secret(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _isolate_runtime_paths(tmp_path, monkeypatch)
    project = project_storage.create_project(
        CreateProjectRequest(title="Leaky Smoke Project", operation_mode="full_auto")
    )
    project_path = Path(project.path)
    _write_smoke_artifacts(project_path)
    (project_path / "chapters" / "chapter-001" / "debug.txt").write_text(
        "provider echoed api-secret at https://api.example.com/v1",
        encoding="utf-8",
    )
    profile_storage.upsert_profile(
        LlmProfileUpsert(
            id="main",
            name="Main Provider",
            protocol="openai-compatible",
            base_url="https://api.example.com/v1",
            api_key="api-secret",
            model="example-model",
        )
    )

    audit = completion_api.get_completion_audit()
    payload = audit.model_dump_json()
    by_id = {gate.id: gate for gate in audit.gates}

    assert audit.status == "failed"
    assert by_id["output_secret_audit"].status == "failed"
    assert "api-secret" not in payload
    assert "https://api.example.com/v1" not in payload


def _isolate_runtime_paths(tmp_path: Path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    output_dir = tmp_path / "output"
    active_project_path = config_dir / "active-project.local.json"
    llm_profiles_path = config_dir / "llm-profiles.local.json"

    monkeypatch.setattr(core_config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(core_config, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(core_config, "ACTIVE_PROJECT_PATH", active_project_path)
    monkeypatch.setattr(core_config, "LLM_PROFILES_PATH", llm_profiles_path)
    monkeypatch.setattr(core_paths, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(project_storage, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(project_storage, "ACTIVE_PROJECT_PATH", active_project_path)
    monkeypatch.setattr(profile_storage, "LLM_PROFILES_PATH", llm_profiles_path)


def _write_smoke_artifacts(project_path: Path, *, status: str = "passed") -> None:
    chapter_path = project_path / "chapters" / "chapter-001"
    exports_path = project_path / "exports"
    chapter_path.mkdir(parents=True, exist_ok=True)
    exports_path.mkdir(parents=True, exist_ok=True)
    artifacts = {
        "final": chapter_path / "final.md",
        "review": chapter_path / "review.md",
        "verification": chapter_path / "verification.json",
        "candidate_state_patch": chapter_path / "candidate_state_patch.json",
        "committed_state_patch": chapter_path / "committed_state_patch.json",
        "manuscript": exports_path / "manuscript.md",
        "smoke_report": exports_path / "live_smoke_report.json",
        "literary_review": exports_path / "literary_review.json",
    }
    for name, path in artifacts.items():
        if name == "literary_review":
            continue
        if path.suffix == ".json":
            path.write_text('{"schema_version":1}\n', encoding="utf-8")
        else:
            path.write_text(f"# {name}\n", encoding="utf-8")

    smoke_report = {
        "status": status,
        "project_name": project_path.name,
        "project_path": str(project_path),
        "profile_id": "main",
        "model_snapshot": "fixture-model",
        "provider_snapshot": "openai-compatible",
        "run_status": "idle",
        "event_count": 8,
        "artifacts": {name: str(path) for name, path in artifacts.items()},
        "manual_review_paths": [
            str(artifacts["final"]),
            str(artifacts["review"]),
            str(artifacts["verification"]),
            str(artifacts["candidate_state_patch"]),
            str(artifacts["committed_state_patch"]),
        ],
    }
    artifacts["smoke_report"].write_text(
        json.dumps(smoke_report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _add_utf8_bom(path: Path) -> None:
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
