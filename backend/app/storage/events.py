from pathlib import Path

from pydantic import ValidationError

from app.schemas.events import HarnessEvent


def append_event(project_path: Path, event: HarnessEvent) -> None:
    events_path = project_path / "events.jsonl"
    events_path.parent.mkdir(parents=True, exist_ok=True)
    event_to_write = event.model_copy(update={"seq": _next_event_seq(project_path)})
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(event_to_write.model_dump_json() + "\n")


def read_events(project_path: Path) -> list[HarnessEvent]:
    events_path = project_path / "events.jsonl"
    if not events_path.exists():
        return []

    events: list[HarnessEvent] = []
    for line in events_path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        try:
            events.append(HarnessEvent.model_validate_json(line))
        except ValidationError:
            continue
    return events


def _next_event_seq(project_path: Path) -> int:
    events = read_events(project_path)
    if not events:
        return 1

    max_seq = max(
        (event.seq if event.seq is not None else index + 1)
        for index, event in enumerate(events)
    )
    return max_seq + 1
