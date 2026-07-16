import pytest

from app.schemas.patches import CandidateStatePatch
from app.storage import patches as patch_storage
from app.storage import transactions
from app.storage.json_files import read_json, write_json
from app.storage.patches import (
    PatchValidationError,
    commit_candidate_state_patch,
    validate_candidate_state_patch,
)


def _make_project(tmp_path):
    project_path = tmp_path / "novel"
    (project_path / "canon").mkdir(parents=True)
    (project_path / "chapters" / "chapter-001").mkdir(parents=True)
    write_json(
        project_path / "canon" / "characters.json",
        {"schema_version": 1, "version": 1, "items": {}},
    )
    (project_path / "chapters" / "chapter-001" / "final.md").write_text(
        "The protagonist chooses to stay and admits they still trust companions.\n",
        encoding="utf-8",
    )
    (project_path / "chapters" / "chapter-001" / "draft.md").write_text(
        "The protagonist chooses to stay and admits they still trust companions.\n",
        encoding="utf-8",
    )
    write_json(
        project_path / "chapters" / "chapter-001" / "observations.json",
        {
            "schema_version": 1,
            "status": "candidate",
            "based_on": "chapters/chapter-001/draft.md",
        },
    )
    return project_path


def _candidate_patch(**operation_overrides) -> CandidateStatePatch:
    operation = {
        "op": "upsert",
        "target_file": "canon/characters.json",
        "target_id": "protagonist",
        "expected_version": 1,
        "value": {"belief": "still trusts companions"},
        "evidence": [
            {
                "file": "chapters/chapter-001/final.md",
                "quote": "still trust companions",
            }
        ],
        "rationale": "The final chapter explicitly changes the protagonist belief.",
    }
    operation.update(operation_overrides)
    return CandidateStatePatch(
        based_on={
            "chapter_final": "chapters/chapter-001/final.md",
            "observations": "chapters/chapter-001/observations.json",
        },
        operations=[operation],
    )


def test_noop_patch_commit_writes_committed_patch_without_mutating_canon(tmp_path) -> None:
    project_path = _make_project(tmp_path)
    committed_path = project_path / "chapters" / "chapter-001" / "committed_state_patch.json"
    patch = CandidateStatePatch(
        based_on={
            "chapter_final": "chapters/chapter-001/final.md",
            "observations": "chapters/chapter-001/observations.json",
        },
        operations=[],
    )

    committed = commit_candidate_state_patch(project_path, patch, committed_path)
    state = read_json(project_path / "canon" / "characters.json")
    committed_payload = read_json(committed_path)

    assert committed.validation.reasons == []
    assert committed.operations == []
    assert committed_payload["operations"] == []
    assert state["version"] == 1
    assert state["items"] == {}


def test_patch_commit_rolls_back_canon_when_multifile_promotion_is_interrupted(
    tmp_path,
    monkeypatch,
) -> None:
    project_path = _make_project(tmp_path)
    committed_path = (
        project_path / "chapters" / "chapter-001" / "committed_state_patch.json"
    )
    original_promote = transactions._promote_staged_file
    promotions = 0

    def fail_second_promotion(staged, target, transaction_id):
        nonlocal promotions
        promotions += 1
        if promotions == 2:
            raise OSError("injected promotion interruption")
        return original_promote(staged, target, transaction_id)

    monkeypatch.setattr(
        transactions,
        "_promote_staged_file",
        fail_second_promotion,
    )

    with pytest.raises(OSError, match="injected promotion interruption"):
        commit_candidate_state_patch(
            project_path,
            _candidate_patch(),
            committed_path,
        )

    assert read_json(project_path / "canon" / "characters.json") == {
        "schema_version": 1,
        "version": 1,
        "items": {},
    }
    assert not committed_path.exists()
    assert not (project_path / "book" / ".transactions").exists()


def test_patch_validation_rejects_disallowed_target(tmp_path) -> None:
    project_path = _make_project(tmp_path)
    patch = _candidate_patch(target_file="book/state.json")

    result = validate_candidate_state_patch(project_path, patch)

    assert result.schema_check == "failed"
    assert "disallowed file" in result.reasons[0]


def test_patch_validation_rejects_stale_expected_version(tmp_path) -> None:
    project_path = _make_project(tmp_path)
    patch = _candidate_patch(expected_version=9)

    result = validate_candidate_state_patch(project_path, patch)

    assert result.versions == "failed"
    assert "expected canon/characters.json version 9" in result.reasons[0]


def test_patch_validation_rejects_missing_evidence(tmp_path) -> None:
    project_path = _make_project(tmp_path)
    patch = _candidate_patch(evidence=[])

    result = validate_candidate_state_patch(project_path, patch)

    assert result.evidence == "failed"
    assert "must include evidence" in result.reasons[0]


def test_patch_validation_rejects_unsafe_based_on_path(tmp_path) -> None:
    project_path = _make_project(tmp_path)
    patch = _candidate_patch()
    patch.based_on = {
        "chapter_final": "../outside.md",
        "observations": "chapters/chapter-001/observations.json",
    }

    result = validate_candidate_state_patch(project_path, patch)

    assert result.evidence == "failed"
    assert any("unsafe path" in reason for reason in result.reasons)


def test_patch_validation_rejects_draft_based_on_source(tmp_path) -> None:
    project_path = _make_project(tmp_path)
    (project_path / "chapters" / "chapter-001" / "draft.md").write_text(
        "Draft-only claim that never reached final.",
        encoding="utf-8",
    )
    patch = _candidate_patch()
    patch.based_on = {
        "chapter_final": "chapters/chapter-001/final.md",
        "observations": "chapters/chapter-001/observations.json",
        "draft": "chapters/chapter-001/draft.md",
    }

    result = validate_candidate_state_patch(project_path, patch)

    assert result.evidence == "failed"
    assert any("unsupported source key: draft" in reason for reason in result.reasons)


def test_patch_validation_rejects_draft_as_chapter_final_source(tmp_path) -> None:
    project_path = _make_project(tmp_path)
    (project_path / "chapters" / "chapter-001" / "draft.md").write_text(
        "Draft-only claim that never reached final.",
        encoding="utf-8",
    )
    patch = _candidate_patch()
    patch.based_on["chapter_final"] = "chapters/chapter-001/draft.md"

    result = validate_candidate_state_patch(project_path, patch)

    assert result.evidence == "failed"
    assert any("must reference final.md" in reason for reason in result.reasons)


def test_patch_validation_requires_observations_source(tmp_path) -> None:
    project_path = _make_project(tmp_path)
    patch = _candidate_patch()
    del patch.based_on["observations"]

    result = validate_candidate_state_patch(project_path, patch)

    assert result.evidence == "failed"
    assert any("must include observations" in reason for reason in result.reasons)


def test_patch_validation_rejects_missing_observations_source(tmp_path) -> None:
    project_path = _make_project(tmp_path)
    patch = _candidate_patch()
    patch.based_on["observations"] = "chapters/chapter-001/missing-observations.json"

    result = validate_candidate_state_patch(project_path, patch)

    assert result.evidence == "failed"
    assert any("must reference observations.json" in reason for reason in result.reasons)
    assert any("source file does not exist" in reason for reason in result.reasons)


def test_patch_validation_rejects_non_candidate_observations_source(tmp_path) -> None:
    project_path = _make_project(tmp_path)
    write_json(
        project_path / "chapters" / "chapter-001" / "observations.json",
        {
            "schema_version": 1,
            "status": "committed",
            "based_on": "chapters/chapter-001/draft.md",
        },
    )

    result = validate_candidate_state_patch(project_path, _candidate_patch())

    assert result.evidence == "failed"
    assert any("status candidate" in reason for reason in result.reasons)


def test_patch_validation_rejects_malformed_observations_source(tmp_path) -> None:
    project_path = _make_project(tmp_path)
    (project_path / "chapters" / "chapter-001" / "observations.json").write_text(
        "{not json",
        encoding="utf-8",
    )

    result = validate_candidate_state_patch(project_path, _candidate_patch())

    assert result.evidence == "failed"
    assert any("not valid JSON" in reason for reason in result.reasons)


def test_patch_validation_rejects_observations_from_other_chapter(tmp_path) -> None:
    project_path = _make_project(tmp_path)
    chapter_two = project_path / "chapters" / "chapter-002"
    chapter_two.mkdir(parents=True)
    (chapter_two / "draft.md").write_text(
        "A different chapter draft.",
        encoding="utf-8",
    )
    write_json(
        chapter_two / "observations.json",
        {
            "schema_version": 1,
            "status": "candidate",
            "based_on": "chapters/chapter-002/draft.md",
        },
    )
    patch = _candidate_patch()
    patch.based_on["observations"] = "chapters/chapter-002/observations.json"

    result = validate_candidate_state_patch(project_path, patch)

    assert result.evidence == "failed"
    assert any("same chapter" in reason for reason in result.reasons)


def test_patch_validation_rejects_observations_based_on_other_chapter_draft(
    tmp_path,
) -> None:
    project_path = _make_project(tmp_path)
    chapter_two = project_path / "chapters" / "chapter-002"
    chapter_two.mkdir(parents=True)
    (chapter_two / "draft.md").write_text(
        "A different chapter draft.",
        encoding="utf-8",
    )
    write_json(
        project_path / "chapters" / "chapter-001" / "observations.json",
        {
            "schema_version": 1,
            "status": "candidate",
            "based_on": "chapters/chapter-002/draft.md",
        },
    )

    result = validate_candidate_state_patch(project_path, _candidate_patch())

    assert result.evidence == "failed"
    assert any("draft must belong to the same chapter" in reason for reason in result.reasons)


def test_patch_validation_rejects_missing_observations_draft_source(tmp_path) -> None:
    project_path = _make_project(tmp_path)
    (project_path / "chapters" / "chapter-001" / "draft.md").unlink()

    result = validate_candidate_state_patch(project_path, _candidate_patch())

    assert result.evidence == "failed"
    assert any("draft file does not exist" in reason for reason in result.reasons)


def test_patch_validation_rejects_observations_as_operation_evidence(tmp_path) -> None:
    project_path = _make_project(tmp_path)
    patch = _candidate_patch(
        evidence=[
            {
                "file": "chapters/chapter-001/observations.json",
                "quote": "trusts companions",
            }
        ]
    )

    result = validate_candidate_state_patch(project_path, patch)

    assert result.evidence == "failed"
    assert any("must cite chapter_final" in reason for reason in result.reasons)


def test_patch_validation_reads_evidence_through_shared_text_helper(tmp_path, monkeypatch) -> None:
    project_path = _make_project(tmp_path)
    read_paths: list[str] = []

    def fake_read_text_file(path):
        read_paths.append(path.relative_to(project_path).as_posix())
        return "The protagonist chooses to stay and admits they still trust companions.\n"

    monkeypatch.setattr(patch_storage, "read_text_file", fake_read_text_file)

    result = validate_candidate_state_patch(project_path, _candidate_patch())

    assert result.evidence == "passed"
    assert read_paths == ["chapters/chapter-001/final.md"]


def test_patch_commit_updates_allowed_canon_file(tmp_path) -> None:
    project_path = _make_project(tmp_path)
    committed_path = project_path / "chapters" / "chapter-001" / "committed_state_patch.json"

    committed = commit_candidate_state_patch(project_path, _candidate_patch(), committed_path)
    state = read_json(project_path / "canon" / "characters.json")
    committed_payload = read_json(committed_path)

    assert committed.validation.reasons == []
    assert committed_payload["status"] == "committed"
    assert state["version"] == 2
    assert state["items"]["protagonist"]["belief"] == "still trusts companions"


def test_patch_commit_raises_without_mutating_on_validation_failure(tmp_path) -> None:
    project_path = _make_project(tmp_path)
    committed_path = project_path / "chapters" / "chapter-001" / "committed_state_patch.json"

    with pytest.raises(PatchValidationError):
        commit_candidate_state_patch(
            project_path,
            _candidate_patch(expected_version=9),
            committed_path,
        )

    state = read_json(project_path / "canon" / "characters.json")
    assert state["version"] == 1
    assert not committed_path.exists()
