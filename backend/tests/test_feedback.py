from app.api import feedback as feedback_api
from app.schemas.events import UserFeedbackRequest
from app.schemas.projects import ProjectMetadata
from app.storage.events import read_events
from app.storage.json_files import write_json


def test_feedback_api_records_user_feedback_event(tmp_path, monkeypatch) -> None:
    project_path = tmp_path / "novel"
    project_path.mkdir()
    write_json(project_path / "project.json", ProjectMetadata(title="Novel").model_dump(mode="json"))
    (project_path / "events.jsonl").write_text("", encoding="utf-8")
    monkeypatch.setattr(feedback_api, "get_active_project_path", lambda: project_path)

    response = feedback_api.submit_feedback(UserFeedbackRequest(message="Make this quieter."))
    events = read_events(project_path)

    assert response == {"recorded": True}
    assert events[-1].kind == "user_feedback"
    assert events[-1].payload["feedback"] == "Make this quieter."
