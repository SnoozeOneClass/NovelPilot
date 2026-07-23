from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from app.domain.routing import RouteSnapshot, decide_route
from app.runtime.live import LossyLiveFanout


def test_route_uses_only_authoritative_compact_facts() -> None:
    snapshot = RouteSnapshot(
        project_id="project-a",
        run_id="run-a",
        run_status="running",
        desired_state="running",
        pending_delivery_task_id="task-a",
    )
    assert decide_route(snapshot).model_dump() == {
        "action": "apply_agent_result",
        "target_id": "task-a",
        "reason_code": "typed_result_requires_domain_command",
    }
    with pytest.raises(ValidationError, match="extra_forbidden"):
        RouteSnapshot.model_validate(
            {
                **snapshot.model_dump(),
                "agent_evidence": {"model_said": "skip approval"},
            }
        )


def test_failure_pause_routes_only_to_explicit_retry() -> None:
    decision = decide_route(
        RouteSnapshot(
            project_id="project-a",
            run_id="run-a",
            run_status="failure_paused",
            desired_state="paused",
            blocking_task_id="failed-task",
        )
    )
    assert decision.action == "await_retry"
    assert decision.target_id == "failed-task"


def test_live_fanout_is_lossy_and_has_no_replay() -> None:
    async def exercise() -> None:
        live: LossyLiveFanout[str] = LossyLiveFanout(queue_size=1)
        subscription = live.subscribe()
        await live.publish("old-delta")
        await live.publish("latest-delta")
        assert await asyncio.wait_for(subscription.__anext__(), timeout=1) == "latest-delta"
        subscription.close()
        assert live.subscriber_count == 0

        # A later subscriber starts empty: historical token deltas are not replayed.
        later = live.subscribe()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(later.__anext__(), timeout=0.01)
        later.close()

    asyncio.run(exercise())
