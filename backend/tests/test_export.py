from app.schemas.projects import ProjectMetadata
from app.storage.events import read_events
from app.storage.json_files import write_json
from app.storage.export import export_manuscript


def test_export_manuscript_uses_committed_chapter_finals_only(tmp_path) -> None:
    project_path = tmp_path / "novel"
    (project_path / "chapters" / "chapter-001").mkdir(parents=True)
    (project_path / "chapters" / "chapter-002").mkdir(parents=True)
    (project_path / "chapters" / "chapter-001" / "draft.md").write_text(
        "draft only",
        encoding="utf-8",
    )
    (project_path / "chapters" / "chapter-001" / "final.md").write_bytes(b"\xef\xbb\xbffinal one")
    (project_path / "chapters" / "chapter-002" / "final.md").write_text(
        "final two",
        encoding="utf-8",
    )

    manuscript_path = export_manuscript(project_path)

    assert manuscript_path.relative_to(project_path).as_posix() == "exports/manuscript.md"
    assert manuscript_path.read_text(encoding="utf-8") == "final one\n\nfinal two\n"


def test_export_api_records_relative_artifact_path(tmp_path, monkeypatch) -> None:
    from app.api import exports

    project_path = tmp_path / "novel"
    (project_path / "chapters" / "chapter-001").mkdir(parents=True)
    (project_path / "chapters" / "chapter-001" / "final.md").write_text(
        "final one",
        encoding="utf-8",
    )
    write_json(project_path / "project.json", ProjectMetadata(title="Novel").model_dump(mode="json"))
    (project_path / "events.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr(exports, "get_active_project_path", lambda: project_path)

    response = exports.export_current_manuscript()
    events = read_events(project_path)

    assert response["artifact_path"] == "exports/manuscript.md"
    assert events[-1].kind == "export_completed"
    assert events[-1].artifact_path == "exports/manuscript.md"
