import pytest
from fastapi import HTTPException

from app.core.paths import resolve_artifact_path
from app.schemas.events import HarnessEvent
from app.storage.artifacts import list_project_artifacts, summarize_project_artifacts
from app.storage.events import append_event
from app.storage.json_files import write_json


def test_artifact_summary_projects_harness_signals(tmp_path) -> None:
    project_path = tmp_path / "novel"
    chapter_path = project_path / "chapters" / "chapter-001"
    chapter_path.mkdir(parents=True)
    arc_path = project_path / "arcs" / "arc-001"
    arc_path.mkdir(parents=True)
    book_path = project_path / "book"
    book_path.mkdir(parents=True)
    (arc_path / "revision.md").write_text("# Arc Revision\n", encoding="utf-8")
    (book_path / "feedback.md").write_text("# Book Feedback\n", encoding="utf-8")
    (chapter_path / "review.md").write_bytes(b"\xef\xbb\xbf# Review\n")
    write_json(
        chapter_path / "attempts" / "attempt-001" / "retry_manifest.json",
        {
            "schema_version": 1,
            "chapter_id": "chapter-001",
            "retry_scope": "chapter_candidate",
            "archived_artifacts": ["attempts/attempt-001/draft.md"],
        },
    )
    write_json(
        chapter_path / "context_snapshot.json",
        {
            "schema_version": 1,
            "chapter_id": "chapter-001",
            "created_at": "2026-01-01T00:00:00Z",
            "sources": [{"id": "book-state", "path": "book/state.json", "usage": "direct"}],
            "excluded": [{"source": "draft", "reason": "candidate"}],
            "assembly_rationale": "Use committed sources only.",
        },
    )
    write_json(
        chapter_path / "verification.json",
        {
            "schema_version": 1,
            "chapter_id": "chapter-001",
            "goal_satisfied": True,
            "commit_allowed": True,
            "routing_decision": "commit",
            "signals": [{"name": "required_artifacts", "status": "passed"}],
            "reasons": [],
        },
    )
    append_event(
        project_path,
        HarnessEvent(
            project_id="project",
            kind="verification_completed",
            loop_layer="chapter",
            atomic_action="verify_chapter",
            artifact_path="chapters/chapter-001/verification.json",
            message="Verification completed.",
            payload={
                "profile_id": "main",
                "model_snapshot": "story-model",
                "base_url": "https://api.example.com/v1",
            },
        ),
    )
    write_json(
        chapter_path / "candidate_state_patch.json",
        {
            "schema_version": 1,
            "status": "candidate",
            "based_on": {},
            "operations": [{"target_file": "canon/characters.json"}],
        },
    )
    write_json(
        chapter_path / "committed_state_patch.json",
        {
            "schema_version": 1,
            "status": "committed",
            "committed_at": "2026-01-01T00:00:00Z",
            "operations": [{"target_file": "canon/characters.json"}],
            "validation": {
                "schema": "passed",
                "versions": "passed",
                "evidence": "passed",
                "conflicts": "passed",
            },
        },
    )

    summaries = summarize_project_artifacts(project_path)
    by_kind = {summary.kind: summary for summary in summaries}

    assert by_kind["context_snapshot"].detail == "1 sources, 1 exclusions"
    assert by_kind["context_snapshot"].event_status == "missing"
    assert by_kind["verification"].status == "passed"
    assert by_kind["verification"].routing_decision == "commit"
    assert by_kind["verification"].event_status == "recorded"
    assert by_kind["verification"].profile_id == "main"
    assert by_kind["verification"].model_snapshot == "story-model"
    assert not hasattr(by_kind["verification"], "base_url")
    assert by_kind["candidate_state_patch"].candidate is True
    assert by_kind["committed_state_patch"].committed is True
    assert by_kind["arc_revision"].status == "revised"
    assert by_kind["book_feedback"].status == "recorded"
    assert by_kind["review"].detail == "Review"
    assert by_kind["retry_manifest"].routing_decision == "retry"


def test_project_artifacts_ignore_internal_temp_files(tmp_path) -> None:
    project_path = tmp_path / "novel"
    chapter_path = project_path / "chapters" / "chapter-001"
    chapter_path.mkdir(parents=True)
    (chapter_path / "draft.md").write_text("draft", encoding="utf-8")
    (chapter_path / "draft.md.tmp").write_text("partial draft", encoding="utf-8")
    write_json(chapter_path / "verification.json.tmp", {"schema_version": 1})

    artifacts = list_project_artifacts(project_path)
    summaries = summarize_project_artifacts(project_path)

    assert artifacts == ["chapters/chapter-001/draft.md"]
    assert [summary.path for summary in summaries] == ["chapters/chapter-001/draft.md"]


def test_artifact_content_api_reads_current_project_file(tmp_path, monkeypatch) -> None:
    from app.api import artifacts

    project_path = tmp_path / "novel"
    chapter_path = project_path / "chapters" / "chapter-001"
    chapter_path.mkdir(parents=True)
    (chapter_path / "final.md").write_bytes(b"\xef\xbb\xbffinal text")
    monkeypatch.setattr(artifacts, "get_active_project_path", lambda: project_path)

    response = artifacts.read_artifact_content("chapters/chapter-001/final.md")

    assert response == {"path": "chapters/chapter-001/final.md", "content": "final text"}


def test_artifact_content_api_hides_internal_temp_files(tmp_path, monkeypatch) -> None:
    from app.api import artifacts

    project_path = tmp_path / "novel"
    chapter_path = project_path / "chapters" / "chapter-001"
    chapter_path.mkdir(parents=True)
    (chapter_path / "draft.md.tmp").write_text("partial draft", encoding="utf-8")
    monkeypatch.setattr(artifacts, "get_active_project_path", lambda: project_path)

    with pytest.raises(HTTPException) as exc:
        artifacts.read_artifact_content("chapters/chapter-001/draft.md.tmp")

    assert exc.value.status_code == 404


def test_artifact_content_api_returns_404_for_safe_missing_path(tmp_path, monkeypatch) -> None:
    from app.api import artifacts

    project_path = tmp_path / "novel"
    project_path.mkdir()
    monkeypatch.setattr(artifacts, "get_active_project_path", lambda: project_path)

    with pytest.raises(HTTPException) as exc:
        artifacts.read_artifact_content("chapters/chapter-001/missing.md")

    assert exc.value.status_code == 404


@pytest.mark.parametrize("path", ["chapters/chapter-001/final.md:ads", "chapters/final.md."])
def test_artifact_content_api_rejects_windows_special_path_spellings(
    tmp_path,
    monkeypatch,
    path: str,
) -> None:
    from app.api import artifacts

    project_path = tmp_path / "novel"
    project_path.mkdir()
    monkeypatch.setattr(artifacts, "get_active_project_path", lambda: project_path)

    with pytest.raises(HTTPException) as exc:
        artifacts.read_artifact_content(path)

    assert exc.value.status_code == 400


@pytest.mark.parametrize(
    "path",
    [
        "",
        ".",
        "..",
        "../secret.txt",
        "/absolute/path.txt",
        "C:escape.txt",
        "chapters\\chapter-001\\final.md",
        "chapters/chapter-001/final.md:ads",
        "chapters/chapter-001/final.md.",
        "chapters/chapter-001/final.md ",
        "chapters/chapter-001 /final.md",
        "chapters/chapter-001/final?.md",
    ],
)
def test_artifact_path_resolution_rejects_project_escape(tmp_path, path: str) -> None:
    with pytest.raises(ValueError):
        resolve_artifact_path(tmp_path / "novel", path)


def test_artifact_content_api_rejects_project_escape(tmp_path, monkeypatch) -> None:
    from app.api import artifacts

    project_path = tmp_path / "novel"
    project_path.mkdir()
    monkeypatch.setattr(artifacts, "get_active_project_path", lambda: project_path)

    with pytest.raises(HTTPException) as exc:
        artifacts.read_artifact_content("../secret.txt")

    assert exc.value.status_code == 400
