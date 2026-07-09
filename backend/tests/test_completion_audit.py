from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core import config as core_config
from app.schemas.profiles import LlmProfileUpsert
from app.storage import profiles as profile_storage
from scripts.completion_audit import build_completion_audit
from scripts.record_literary_review import record_literary_review


def test_completion_audit_is_pending_without_live_smoke_report(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]

    audit = build_completion_audit(repo_root, smoke_report_path=tmp_path / "missing.json")
    by_id = {gate.id: gate for gate in audit.gates}

    assert audit.status == "pending"
    assert by_id["static_acceptance"].status == "passed"
    assert by_id["output_secret_audit"].status == "passed"
    assert by_id["live_provider_smoke"].status == "pending"
    assert by_id["literary_quality_review"].status == "pending"


def test_completion_audit_passes_after_smoke_and_literary_review(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    project_path = _write_smoke_project(tmp_path)

    before_review = build_completion_audit(repo_root, project_path=project_path)
    by_id = {gate.id: gate for gate in before_review.gates}

    assert before_review.status == "pending"
    assert by_id["output_secret_audit"].status == "passed"
    assert by_id["live_provider_smoke"].status == "passed"
    assert by_id["literary_quality_review"].status == "pending"

    review = record_literary_review(
        project_path=project_path,
        decision="approved",
        reviewer="test reviewer",
        chapter_assessment="The generated chapter is coherent enough for the smoke gate.",
        state_patch_assessment="The state patch has evidence and matches the final chapter.",
        notes="Fixture review.",
    )
    after_review = build_completion_audit(repo_root, project_path=project_path)

    assert Path(str(review["literary_review_json"])).exists()
    assert Path(str(review["literary_review_markdown"])).exists()
    assert after_review.status == "passed"
    assert all(gate.status == "passed" for gate in after_review.gates)


def test_completion_audit_rejects_smoke_artifacts_outside_project(
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    project_path = _write_smoke_project(tmp_path)
    outside_final = tmp_path / "outside-final.md"
    outside_final.write_text("# outside\n", encoding="utf-8")
    smoke_report_path = project_path / "exports" / "live_smoke_report.json"
    smoke_report = json.loads(smoke_report_path.read_text(encoding="utf-8"))
    smoke_report["artifacts"]["final"] = str(outside_final)
    smoke_report_path.write_text(
        json.dumps(smoke_report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    audit = build_completion_audit(repo_root, project_path=project_path)
    by_id = {gate.id: gate for gate in audit.gates}

    assert audit.status == "failed"
    assert by_id["live_provider_smoke"].status == "failed"
    assert "outside the smoke project" in by_id["live_provider_smoke"].message

    with pytest.raises(ValueError, match="outside the smoke project"):
        record_literary_review(
            project_path=project_path,
            decision="approved",
            reviewer="test reviewer",
            chapter_assessment="Looks coherent.",
            state_patch_assessment="Patch is evidence-backed.",
            notes="",
        )


def test_literary_review_refuses_failed_live_smoke_report(tmp_path: Path) -> None:
    project_path = _write_smoke_project(tmp_path)
    smoke_report_path = project_path / "exports" / "live_smoke_report.json"
    smoke_report = json.loads(smoke_report_path.read_text(encoding="utf-8"))
    smoke_report["status"] = "failed"
    smoke_report_path.write_text(
        json.dumps(smoke_report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="live provider smoke passes"):
        record_literary_review(
            project_path=project_path,
            decision="approved",
            reviewer="test reviewer",
            chapter_assessment="Looks coherent.",
            state_patch_assessment="Patch is evidence-backed.",
            notes="",
        )

    assert not (project_path / "exports" / "literary_review.json").exists()


def test_completion_audit_reports_failed_live_smoke_diagnostics(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    project_path = _write_smoke_project(tmp_path)
    smoke_report_path = project_path / "exports" / "live_smoke_report.json"
    smoke_report = json.loads(smoke_report_path.read_text(encoding="utf-8"))
    smoke_report["status"] = "failed"
    smoke_report["failure"] = {
        "message": "Harness did not commit a state patch.",
        "project_path": str(project_path),
        "last_event": {
            "kind": "state_patch_rejected",
            "atomic_action": "generate_candidate_state_patch",
            "status": "failed",
            "routing_decision": "pause",
            "artifact_path": "chapters/chapter-001/state_patch_rejection.json",
        },
        "artifact_reasons": ["State patch generator output could not be parsed as JSON."],
    }
    smoke_report_path.write_text(
        json.dumps(smoke_report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    audit = build_completion_audit(repo_root, project_path=project_path)
    by_id = {gate.id: gate for gate in audit.gates}

    assert audit.status == "failed"
    assert by_id["live_provider_smoke"].status == "failed"
    assert "Harness did not commit a state patch." in by_id["live_provider_smoke"].message
    assert any("state_patch_rejected" in item for item in by_id["live_provider_smoke"].evidence)
    assert any("could not be parsed as JSON" in item for item in by_id["live_provider_smoke"].evidence)


def test_completion_audit_fails_when_output_contains_profile_secret(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    _isolate_profile_path(tmp_path, monkeypatch)
    project_path = _write_smoke_project(tmp_path)
    (project_path / "chapters" / "chapter-001" / "debug.txt").write_text(
        "provider echoed completion-secret at https://completion.example.com/v1",
        encoding="utf-8",
    )
    profile_storage.upsert_profile(
        LlmProfileUpsert(
            id="main",
            name="Main Provider",
            protocol="openai-compatible",
            base_url="https://completion.example.com/v1",
            api_key="completion-secret",
            model="example-model",
        )
    )

    audit = build_completion_audit(repo_root, project_path=project_path)
    payload = json.dumps(audit.to_dict(), ensure_ascii=False)
    by_id = {gate.id: gate for gate in audit.gates}

    assert audit.status == "failed"
    assert by_id["output_secret_audit"].status == "failed"
    assert "completion-secret" not in payload
    assert "https://completion.example.com/v1" not in payload


def _isolate_profile_path(tmp_path: Path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    monkeypatch.setattr(core_config, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(core_config, "OUTPUT_DIR", tmp_path / "output")
    monkeypatch.setattr(
        profile_storage,
        "LLM_PROFILES_PATH",
        config_dir / "llm-profiles.local.json",
    )


def _write_smoke_project(tmp_path: Path) -> Path:
    project_path = tmp_path / "Novelpilot Live Smoke Fixture"
    chapter_path = project_path / "chapters" / "chapter-001"
    exports_path = project_path / "exports"
    chapter_path.mkdir(parents=True)
    exports_path.mkdir(parents=True)

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
        "status": "passed",
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
    return project_path
