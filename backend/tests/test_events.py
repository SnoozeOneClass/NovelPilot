import json

from app.schemas.events import HarnessEvent
from app.storage.events import append_event, read_events


def test_read_events_accepts_utf8_bom(tmp_path) -> None:
    project_path = tmp_path / "novel"
    event = HarnessEvent(project_id="project", kind="run_started", message="Started.")
    events_path = project_path / "events.jsonl"
    events_path.parent.mkdir(parents=True)
    events_path.write_bytes(b"\xef\xbb\xbf" + event.model_dump_json().encode("utf-8") + b"\n")

    events = read_events(project_path)

    assert [item.event_id for item in events] == [event.event_id]


def test_append_event_does_not_write_utf8_bom(tmp_path) -> None:
    project_path = tmp_path / "novel"

    append_event(
        project_path,
        HarnessEvent(project_id="project", kind="run_started", message="Started."),
    )

    assert not (project_path / "events.jsonl").read_bytes().startswith(b"\xef\xbb\xbf")


def test_append_event_assigns_monotonic_seq(tmp_path) -> None:
    project_path = tmp_path / "novel"

    append_event(project_path, HarnessEvent(project_id="project", kind="one", message="One."))
    append_event(project_path, HarnessEvent(project_id="project", kind="two", message="Two."))

    events = read_events(project_path)

    assert [event.seq for event in events] == [1, 2]


def test_read_events_accepts_legacy_events_without_seq(tmp_path) -> None:
    project_path = tmp_path / "novel"
    event = HarnessEvent(project_id="project", kind="legacy", message="Legacy.")
    payload = event.model_dump(mode="json")
    payload.pop("seq")
    events_path = project_path / "events.jsonl"
    events_path.parent.mkdir(parents=True)
    events_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    append_event(project_path, HarnessEvent(project_id="project", kind="new", message="New."))
    events = read_events(project_path)

    assert [event.kind for event in events] == ["legacy", "new"]
    assert [event.seq for event in events] == [None, 2]


def test_read_events_skips_invalid_lines_and_keeps_later_events(tmp_path) -> None:
    project_path = tmp_path / "novel"
    first = HarnessEvent(project_id="project", kind="first", message="First.")
    second = HarnessEvent(project_id="project", kind="second", message="Second.")
    events_path = project_path / "events.jsonl"
    events_path.parent.mkdir(parents=True)
    events_path.write_text(
        first.model_dump_json() + "\nnot-json\n" + second.model_dump_json() + "\n",
        encoding="utf-8",
    )

    events = read_events(project_path)

    assert [event.kind for event in events] == ["first", "second"]


def test_append_event_owns_seq_even_if_caller_supplies_one(tmp_path) -> None:
    project_path = tmp_path / "novel"

    append_event(
        project_path,
        HarnessEvent(seq=999, project_id="project", kind="run_started", message="Started."),
    )

    assert read_events(project_path)[0].seq == 1
