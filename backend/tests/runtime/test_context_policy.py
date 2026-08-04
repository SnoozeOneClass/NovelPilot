from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from alembic import command
from sqlalchemy import select

from app.db.engine import create_sqlite_async_engine
from app.db.maintenance import alembic_config
from app.db.schema import book_workspaces
from app.domain.feedback import (
    ApplyFeedbackRequest,
    FeedbackCommandService,
    QueueFeedbackRequest,
)
from app.runtime.context import (
    CONTEXT_POLICY_REGISTRY,
    ContextFactError,
    HarnessContextBuilder,
    _ContextItem,
)
from app.store.command_bus import CommandBus
from tests.helpers.lifecycle_seed import seed_approved_book_and_arc


def _item_for_policy(task_kind: str, group: str) -> _ContextItem:
    block = CONTEXT_POLICY_REGISTRY[task_kind].block_for(group)
    return _ContextItem(
        group=group,
        label=f"{group}_test",
        role=block.role,
        scope=block.scope,
        time=block.time,
        use=block.use,
        access=block.access,
        target=block.target,
        content_sha256="a" * 64,
        semantic_kind="application/json",
        text="{}",
        sources=(),
    )


def test_every_context_policy_is_task_specific_and_cxt1_complete() -> None:
    assert CONTEXT_POLICY_REGISTRY
    for task_kind, policy in CONTEXT_POLICY_REGISTRY.items():
        assert policy.task_kind == task_kind
        assert len(policy.blocks) == len({block.group for block in policy.blocks})
        assert policy.required_groups.issubset(policy.allowed_groups)
        assert all(
            alternatives.issubset(policy.allowed_groups)
            for alternatives in policy.required_any_groups
        )
        for block in policy.blocks:
            assert block.role
            assert block.scope in {"book", "arc", "chapter", "canon", "system"}
            assert block.time
            assert block.use
            assert block.access in {"read_only", "writable_target"}
        target_blocks = [block for block in policy.blocks if block.target]
        assert len(target_blocks) == (1 if policy.requires_target else 0)

    assert "committed_observations" not in (
        CONTEXT_POLICY_REGISTRY["arc.plan"].allowed_groups
    )
    assert "committed_observations" in (
        CONTEXT_POLICY_REGISTRY["evaluate.arc"].allowed_groups
    )
    assert "committed_observations" in (
        CONTEXT_POLICY_REGISTRY["verify_repair.arc"].allowed_groups
    )
    assert "book_completion_contract" not in (
        CONTEXT_POLICY_REGISTRY["arc.plan"].allowed_groups
    )
    assert "book_arc_topology" not in (
        CONTEXT_POLICY_REGISTRY["arc.plan"].allowed_groups
    )
    assert "book_discussion_state" not in (
        CONTEXT_POLICY_REGISTRY["book.repair"].allowed_groups
    )
    assert "book_discussion_transcript" in (
        CONTEXT_POLICY_REGISTRY["book.discuss"].allowed_groups
    )
    assert "book_discussion_transcript" not in (
        CONTEXT_POLICY_REGISTRY["book.synthesize"].allowed_groups
    )
    assert "chapter_candidate_review" not in (
        CONTEXT_POLICY_REGISTRY["chapter.observe"].allowed_groups
    )
    assert "committed_observations" in (
        CONTEXT_POLICY_REGISTRY["evaluate.chapter"].allowed_groups
    )
    assert "committed_observations" in (
        CONTEXT_POLICY_REGISTRY["verify_repair.chapter"].allowed_groups
    )
    for task_kind, guidance_group in (
        ("arc.plan", "arc_guidance"),
        ("evaluate.arc", "arc_guidance"),
        ("chapter.plan", "chapter_guidance"),
        ("chapter.draft", "chapter_guidance"),
        ("evaluate.chapter", "chapter_guidance"),
    ):
        assert guidance_group in CONTEXT_POLICY_REGISTRY[task_kind].allowed_groups
    assert "chapter_guidance" not in (
        CONTEXT_POLICY_REGISTRY["chapter.observe"].allowed_groups
    )
    assert "arc_chapter_window" not in (
        CONTEXT_POLICY_REGISTRY["chapter.observe"].allowed_groups
    )
    assert "committed_observations" not in (
        CONTEXT_POLICY_REGISTRY["evaluate.arc_closure"].allowed_groups
    )
    assert {
        "book_completion_contract",
        "book_arc_topology",
    }.issubset(
        CONTEXT_POLICY_REGISTRY["evaluate.book_completion"].allowed_groups
    )
    assert (
        CONTEXT_POLICY_REGISTRY["chapter.repair.plan"]
        .block_for("context_target")
        .access
        == "writable_target"
    )
    for task_kind in (
        "book.repair",
        "verify_repair.book",
        "arc.repair",
        "verify_repair.arc",
        "chapter.repair.observation",
        "verify_repair.chapter",
        "verify_evidence.chapter",
    ):
        policy = CONTEXT_POLICY_REGISTRY[task_kind]
        if "canon" in policy.allowed_groups:
            assert policy.block_for("canon").time == "current"
    for task_kind in (
        "verify_repair.book",
        "verify_repair.arc",
        "verify_repair.chapter",
    ):
        policy = CONTEXT_POLICY_REGISTRY[task_kind]
        assert not any(
            block.role == "working_candidate" and block.time == "pre_repair"
            for block in policy.blocks
        )
    assert (
        CONTEXT_POLICY_REGISTRY["verify_repair.book"]
        .block_for("book_pre_repair_candidate")
        .role
        == "comparison_snapshot"
    )
    assert (
        CONTEXT_POLICY_REGISTRY["verify_repair.arc"]
        .block_for("arc_pre_repair_candidate")
        .role
        == "comparison_snapshot"
    )
    assert (
        CONTEXT_POLICY_REGISTRY["chapter.repair.observation"]
        .block_for("chapter_canon_patch")
        .time
        == "pre_repair"
    )
    assert (
        CONTEXT_POLICY_REGISTRY["verify_repair.chapter"]
        .block_for("chapter_canon_patch")
        .time
        == "post_repair"
    )
    assert CONTEXT_POLICY_REGISTRY["arc.plan"].block_for("book_handoff").scope == "book"
    assert (
        CONTEXT_POLICY_REGISTRY["evaluate.arc_parent_contract"]
        .block_for("chapter_arc_request")
        .scope
        == "arc"
    )
    for task_kind in (
        "chapter.revise.plan",
        "chapter.revise.draft",
        "chapter.revise.observe",
    ):
        assert {
            "arc_parent_review",
            "arc_closure_review",
        }.issubset(CONTEXT_POLICY_REGISTRY[task_kind].allowed_groups)


def test_context_policy_fails_closed_for_missing_target_and_writable_authority() -> None:
    policy = CONTEXT_POLICY_REGISTRY["evaluate.chapter"]
    items = [
        _item_for_policy("evaluate.chapter", block.group)
        for block in policy.blocks
        if block.group != "context_target"
    ]
    with pytest.raises(ContextFactError, match="requires 1 target"):
        policy.validate(items)

    repair_policy = CONTEXT_POLICY_REGISTRY["chapter.repair.plan"]
    valid = [
        _item_for_policy("chapter.repair.plan", block.group)
        for block in repair_policy.blocks
    ]
    formal_index = next(
        index for index, item in enumerate(valid) if item.role == "formal_contract"
    )
    invalid = list(valid)
    invalid[formal_index] = replace(
        invalid[formal_index],
        access="writable_target",
    )
    with pytest.raises(ContextFactError, match="does not match its CXT1 policy"):
        repair_policy.validate(invalid)

    missing_required = [
        _item_for_policy("evaluate.chapter", block.group)
        for block in policy.blocks
        if block.group != "canon"
    ]
    with pytest.raises(ContextFactError, match="missing required groups: canon"):
        policy.validate(missing_required)

    revision_policy = CONTEXT_POLICY_REGISTRY["chapter.revise.observe"]
    missing_authorization = [
        _item_for_policy("chapter.revise.observe", block.group)
        for block in revision_policy.blocks
        if block.group
        not in {"chapter_guidance", "arc_parent_review", "arc_closure_review"}
    ]
    with pytest.raises(ContextFactError, match="requires one of"):
        revision_policy.validate(missing_authorization)


def test_evaluator_context_has_one_semantic_target_and_hides_internal_ids(
    tmp_path: Path,
) -> None:
    database = tmp_path / "cxt1-evaluator.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            foundation = await seed_approved_book_and_arc(
                engine,
                project_id="project-cxt1-target",
                target_chapter_count=2,
            )
            context = await HarnessContextBuilder(engine).build(
                task_kind="evaluate.arc",
                project_id=foundation.project_id,
                book_id=foundation.book_id,
                arc_id=foundation.arc_id,
                chapter_id=None,
                semantic_goal="Evaluate the current Arc candidate.",
            )
            policy = cast(
                dict[str, object],
                context.manifest["context_policy"],
            )
            items = cast(list[dict[str, object]], context.manifest["items"])
            targets = [item for item in items if item["target"] is True]
            assert policy["cxt_contract"] == "CXT1"
            assert policy["target_count"] == 1
            assert policy["required_groups"] == [
                "arc_outline_projection",
                "arc_working",
                "book_baseline",
                "canon",
            ]
            assert policy["required_any_groups"] == []
            assert len(targets) == 1
            assert targets[0] == {
                **targets[0],
                "role": "target_descriptor",
                "scope": "arc",
                "time": "current",
                "use": "evaluation_target",
                "access": "read_only",
                "target": True,
            }
            assert (
                '<NOVELPILOT_CONTEXT role="target_descriptor" '
                'scope="arc" time="current" use="evaluation_target" '
                'access="read_only" target="true">'
            ) in context.prompt
            assert foundation.book_baseline_id not in context.prompt
            assert foundation.arc_baseline_id not in context.prompt
            assert foundation.canon_baseline_id not in context.prompt
            assert '"assigned_completion_requirements"' in context.prompt
            assert '"requirement_key":"memory_conflict_resolved"' in context.prompt
        finally:
            await engine.dispose()

    asyncio.run(exercise())


def test_book_successor_context_labels_history_without_reusing_old_topology_counters(
    tmp_path: Path,
) -> None:
    database = tmp_path / "cxt1-book-successor.sqlite3"
    command.upgrade(alembic_config(database), "head")

    async def exercise() -> None:
        engine = create_sqlite_async_engine(database)
        try:
            foundation = await seed_approved_book_and_arc(
                engine,
                project_id="project-cxt1-successor",
                target_chapter_count=2,
                arc_contract_count=4,
            )
            feedback_service = FeedbackCommandService(CommandBus(engine))
            feedback = await feedback_service.queue(
                QueueFeedbackRequest(
                    project_id=foundation.project_id,
                    content="Clarify only the mutable future Book direction.",
                    route_layer="book",
                    book_id=foundation.book_id,
                ),
                idempotency_key="cxt1-successor:queue",
            )
            async with engine.connect() as connection:
                workspace_lock = await connection.scalar(
                    select(book_workspaces.c.lock_version).where(
                        book_workspaces.c.book_id == foundation.book_id
                    )
                )
            assert workspace_lock is not None
            await feedback_service.apply(
                ApplyFeedbackRequest(
                    project_id=foundation.project_id,
                    feedback_id=feedback.result.feedback_id,
                    expected_workspace_lock_version=workspace_lock,
                ),
                idempotency_key="cxt1-successor:apply",
            )

            context = await HarnessContextBuilder(engine).build(
                task_kind="book.revise",
                project_id=foundation.project_id,
                book_id=foundation.book_id,
                arc_id=None,
                chapter_id=None,
                semantic_goal="Revise only the mutable future Book contract.",
            )
            state_marker = "Model-visible semantic state and counters:\n"
            visible_state = next(
                line
                for line in context.prompt.split(state_marker, maxsplit=1)[1].splitlines()
                if line.strip()
            )
            assert '"candidate_kind":"successor"' in visible_state
            assert '"historical_prefix_arc_count":0' in visible_state
            assert '"book_arc_contract_count"' not in visible_state
            assert '"book_final_arc_ordinal"' not in visible_state
            assert '"topology_effective_after_arc_ordinal"' not in visible_state
        finally:
            await engine.dispose()

    asyncio.run(exercise())
