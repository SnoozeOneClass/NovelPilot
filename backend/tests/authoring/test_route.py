from app.authoring.domain.models import Phase, RunStatus, StateSnapshot, TargetLength
from app.authoring.runtime.routing import route


def _state(**changes: object) -> StateSnapshot:
    values: dict[str, object] = {
        "project_id": "p1",
        "status": RunStatus.RUNNING,
        "phase": Phase.WRITING,
        "target": TargetLength.resolve(target_chapters=3),
        "fact_version": "facts-1",
        "foundation_present": True,
        "foundation_audited": True,
        "chapter_count": 0,
        "planned_through": 3,
        "reviewed_through": 0,
        "summarized_through": 0,
    }
    values.update(changes)
    return StateSnapshot.model_validate(values)


def test_route_is_pure_and_uses_stable_instruction_identity() -> None:
    snapshot = _state()

    first = route(snapshot)
    second = route(snapshot)

    assert first == second
    assert first is not None
    assert first.logical_target == "chapter:1"
    assert first.instruction_key == second.instruction_key


def test_route_prioritizes_rewrite_review_summary_and_completion() -> None:
    rewrite = route(_state(chapter_count=3, pending_rewrites=(2,)))
    review = route(_state(chapter_count=3))
    summary = route(_state(chapter_count=3, reviewed_through=3))
    complete = route(_state(chapter_count=3, reviewed_through=3, summarized_through=3))

    assert rewrite and rewrite.kind.value == "rewrite_chapter"
    assert review and review.kind.value == "review_boundary"
    assert summary and summary.kind.value == "save_summary"
    assert complete and complete.kind.value == "complete_book"


def test_route_stops_for_control_states() -> None:
    for status in (
        RunStatus.PAUSED,
        RunStatus.FAILURE_PAUSED,
        RunStatus.CANCELLED,
        RunStatus.COMPLETED,
    ):
        assert route(_state(status=status)) is None
