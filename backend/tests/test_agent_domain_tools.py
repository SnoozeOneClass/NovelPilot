from pathlib import Path

from app.harness.agents.domain_tools import build_default_tool_registry
from app.harness.agents.models import (
    AgentIdentity,
    RepairContract,
    StoryArcCandidateSnapshot,
)
from app.harness.agents.rubrics import component_fingerprints
from app.harness.agents.registry import ToolExecutionContext
from app.llm.gateway import ToolCall
from app.storage.json_files import read_json, write_json


def test_provider_strict_tool_schemas_require_defaulted_properties() -> None:
    registry = build_default_tool_registry()
    context_schema = registry.definitions(
        role="book",
        phase="discussion",
        names=["get_loop_context"],
    )[0].input_schema
    discussion_schema = registry.definitions(
        role="book",
        phase="discussion",
        names=["submit_book_discussion_update"],
    )[0].input_schema

    assert context_schema["required"] == ["pack", "max_characters"]
    assert "default" not in context_schema["properties"]["max_characters"]
    suggestion_schema = discussion_schema["$defs"]["SetupSuggestion"]
    assert suggestion_schema["required"] == [
        "id",
        "label",
        "message",
        "rationale",
        "recommended",
    ]
    assert "default" not in suggestion_schema["properties"]["recommended"]


def test_chapter_submission_tool_schema_contains_no_open_json_objects() -> None:
    schema = build_default_tool_registry().definitions(
        role="chapter",
        phase="chapter",
        names=["submit_chapter_candidate"],
    )[0].input_schema

    def assert_closed(value: object) -> None:
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert value.get("additionalProperties") is False
            for child in value.values():
                assert_closed(child)
        elif isinstance(value, list):
            for child in value:
                assert_closed(child)

    assert_closed(schema)


def test_book_direction_submission_is_candidate_only_and_revision_guarded(
    tmp_path: Path,
) -> None:
    registry = build_default_tool_registry()
    arguments = {
        "expected_revision": 4,
        "candidate_revision": 2,
        "direction_markdown": "# 方向\n\n候选方向。",
        "constraints": {
            "confirmed": ["双故事弧"],
            "must_preserve": [],
            "must_avoid": [],
            "creative_freedoms": [],
            "open_decisions": [],
        },
        "confirmed_decision_coverage": [
            {"decision": "双故事弧", "candidate_evidence": "方向第一节"}
        ],
        "recommended_titles": [
            {"title": "潮汐一", "rationale": "理由一"},
            {"title": "潮汐二", "rationale": "理由二"},
            {"title": "潮汐三", "rationale": "理由三"},
        ],
        "rolling_plan_markdown": "先规划第一故事弧。",
    }

    stale = registry.execute(
        _context(
            tmp_path,
            role="book",
            phase="direction",
            revision=3,
            call_id="stale",
            expected_candidate_revision=2,
        ),
        _call("stale", "submit_book_direction_candidate", arguments),
    )
    wrong_candidate_revision = registry.execute(
        _context(
            tmp_path,
            role="book",
            phase="direction",
            revision=4,
            call_id="wrong-candidate-revision",
            expected_candidate_revision=1,
        ),
        _call(
            "wrong-candidate-revision",
            "submit_book_direction_candidate",
            arguments,
        ),
    )
    missing_candidate_target = registry.execute(
        _context(
            tmp_path,
            role="book",
            phase="direction",
            revision=4,
            call_id="missing-candidate-target",
        ),
        _call(
            "missing-candidate-target",
            "submit_book_direction_candidate",
            arguments,
        ),
    )

    candidate_path = (
        tmp_path / "book" / "agent" / "a" / "activation-1" / "c" / "book-direction.json"
    )
    assert not candidate_path.exists()
    accepted = registry.execute(
        _context(
            tmp_path,
            role="book",
            phase="direction",
            revision=4,
            call_id="ok",
            expected_candidate_revision=2,
        ),
        _call("ok", "submit_book_direction_candidate", arguments),
    )

    assert stale.status == "error"
    assert stale.error_code == "stale_candidate_revision"
    assert stale.recoverable is True
    assert stale.content == {"expected_revision": 3, "received_revision": 4}
    assert wrong_candidate_revision.status == "error"
    assert wrong_candidate_revision.error_code == "stale_candidate_revision"
    assert wrong_candidate_revision.recoverable is True
    assert wrong_candidate_revision.content == {
        "expected_candidate_revision": 1,
        "received_candidate_revision": 2,
    }
    assert missing_candidate_target.status == "error"
    assert missing_candidate_target.error_code == "missing_expected_candidate_revision"
    assert missing_candidate_target.recoverable is False
    assert accepted.status == "ok"
    assert accepted.terminal is True
    assert accepted.content["promotable"] is False
    assert not (tmp_path / "book" / "direction.md").exists()
    payload = read_json(tmp_path / accepted.artifact_paths[0])
    assert payload["candidate_revision"] == 2


def test_book_discussion_tool_rejects_delegated_topic_selection() -> None:
    registry = build_default_tool_registry()
    result = registry.execute(
        _context(
            Path("."),
            role="book",
            phase="discussion",
            revision=0,
            call_id="discussion",
        ),
        _call(
            "discussion",
            "submit_book_discussion_update",
            {
                "expected_revision": 0,
                "reply": "The cast boundary is unresolved.",
                "direction_draft": "# Direction\n\nA bounded mystery direction.",
                "discussion_summary": "The user wants a fair mystery.",
                "confirmed_decisions": ["Clues remain fair."],
                "superseded_decisions": [],
                "unresolved_questions": ["Who belongs to the six-person cast?"],
                "assumptions": [],
                "contradictions": [],
                "question": "Which issue should we discuss first?",
                "suggestions": [
                    {
                        "id": "cast",
                        "label": "Clarify cast",
                        "message": "Clarify who counts among the six.",
                        "rationale": "This blocks downstream relationships.",
                        "recommended": True,
                    },
                    {
                        "id": "motive",
                        "label": "Choose motive",
                        "message": "Choose the old-case motive first.",
                        "rationale": "This establishes the investigation pressure.",
                        "recommended": False,
                    },
                ],
                "readiness": {
                    "status": "continue",
                    "reason": "A foundational ambiguity remains.",
                },
            },
        ),
    )

    assert result.status == "error"
    assert result.error_code == "invalid_tool_arguments"
    assert "choose the next concrete decision" in str(result.content["issues"])
    serialized = result.model_dump_json()
    assert "ValueError" not in serialized


def test_story_arc_tool_enforces_agent_ownership(tmp_path: Path) -> None:
    registry = build_default_tool_registry()
    result = registry.execute(
        _context(
            tmp_path,
            role="story_arc",
            scope_id="arc-0001",
            phase="planning",
            revision=0,
            call_id="arc-call",
        ),
        _call(
            "arc-call",
            "submit_story_arc_candidate",
            {
                "expected_revision": 0,
                "intent": "create",
                "arc_id": "arc-0002",
                "plan_markdown": "# 错误归属",
                "target_chapter_count": 10,
                "change_summary": "新建故事弧。",
            },
        ),
    )

    assert result.status == "error"
    assert result.error_code == "arc_ownership_mismatch"


def test_story_arc_repair_rejects_changes_outside_authorized_components(
    tmp_path: Path,
) -> None:
    source = StoryArcCandidateSnapshot(
        plan="# Arc\n\nOriginal plan.",
        target_chapter_count=10,
        change_summary="Create the arc.",
    )
    contract = RepairContract(
        evaluation_id="evaluation-1",
        source_activation_id="source-activation",
        source_candidate_artifact_id="arcs/arc-0001/source.json",
        source_candidate_revision=1,
        next_candidate_revision=2,
        open_issue_ids=["issue-1"],
        repair_brief="Repair only the plan.",
        allowed_components=["plan"],
        source_component_fingerprints=component_fingerprints(source),
    )
    result = build_default_tool_registry().execute(
        _context(
            tmp_path,
            role="story_arc",
            scope_id="arc-0001",
            phase="planning",
            revision=0,
            call_id="repair-scope",
            repair_contract=contract,
        ),
        _call(
            "repair-scope",
            "submit_story_arc_candidate",
            {
                "expected_revision": 0,
                "intent": "create",
                "arc_id": "arc-0001",
                "plan_markdown": "# Arc\n\nRepaired plan.",
                "target_chapter_count": 11,
                "change_summary": "Create the arc.",
            },
        ),
    )

    assert result.status == "error"
    assert result.error_code == "candidate_repair_scope_violation"
    assert result.recoverable is True
    assert result.content["unexpected_components"] == ["target_chapter_count"]


def test_chapter_tools_build_quarantined_candidate_and_never_promote(
    tmp_path: Path,
) -> None:
    registry = build_default_tool_registry()
    context = _context(
        tmp_path,
        role="chapter",
        scope_id="chapter-0001",
        phase="chapter",
        revision=0,
        call_id="plan",
    )
    plan = registry.execute(
        context,
        _call(
            "plan",
            "plan_chapter_candidate",
            {
                "chapter_id": "chapter-0001",
                "expected_revision": 0,
                "plan_revision": 1,
                "plan_markdown": "# 第一章计划",
            },
        ),
    )
    draft = registry.execute(
        context.__class__(**{**context.__dict__, "tool_call_id": "draft"}),
        _call(
            "draft",
            "write_chapter_draft",
            {
                "chapter_id": "chapter-0001",
                "expected_revision": 0,
                "plan_revision": 1,
                "draft_revision": 1,
                "mode": "write",
                "content": "钟声响起。所有人都看向门口。",
            },
        ),
    )
    inspect = registry.execute(
        context.__class__(**{**context.__dict__, "tool_call_id": "inspect"}),
        _call(
            "inspect",
            "inspect_chapter_consistency",
            {
                "chapter_id": "chapter-0001",
                "expected_revision": 0,
                "draft_revision": 1,
            },
        ),
    )
    submit = registry.execute(
        context.__class__(**{**context.__dict__, "tool_call_id": "submit"}),
        _call(
            "submit",
            "submit_chapter_candidate",
            {
                "chapter_id": "chapter-0001",
                "expected_revision": 0,
                "candidate_revision": 1,
                "plan_revision": 1,
                "draft_revision": 1,
                "summary": "第一章候选。",
                "observations": {
                    "events": [
                        {
                            "summary": "The bell exposes the arrival.",
                            "evidence_quote": "The bell",
                        }
                    ],
                    "character_changes": [],
                    "relationship_changes": [],
                    "world_fact_candidates": [],
                    "foreshadowing_candidates": [],
                    "requires_commit": True,
                },
                "state_patch": {
                    "operations": [
                        {
                            "op": "upsert",
                            "target_file": "canon/world_facts.json",
                            "target_id": "bell-arrival",
                            "expected_version": 1,
                            "value_fields": [
                                {"key": "visible", "json_value": "true"},
                                {"key": "name", "json_value": "harbor bell"},
                            ],
                            "evidence_quotes": ["钟声响起"],
                            "rationale": "The draft makes the bell audible.",
                        }
                    ],
                },
            },
        ),
    )

    assert plan.status == "ok"
    assert draft.status == "ok"
    assert inspect.status == "ok"
    assert inspect.content["semantic_verdict"] is None
    assert submit.status == "ok"
    assert submit.terminal is True
    assert submit.content["promotable"] is False
    assert not (tmp_path / "chapters" / "chapter-0001" / "final.md").exists()
    manifest_path = next(
        path for path in submit.artifact_paths if path.endswith("manifest.json")
    )
    manifest = read_json(tmp_path / manifest_path)
    assert manifest["promotable"] is False
    assert manifest["state_patch"]["operations"][0]["value"] == {
        "visible": True,
        "name": "harbor bell",
    }


def test_chapter_submission_rejects_non_verbatim_patch_evidence_before_checkpoint(
    tmp_path: Path,
) -> None:
    registry = build_default_tool_registry()
    context = _context(
        tmp_path,
        role="chapter",
        scope_id="chapter-0001",
        phase="chapter",
        revision=0,
        call_id="plan",
    )
    registry.execute(
        context,
        _call(
            "plan",
            "plan_chapter_candidate",
            {
                "chapter_id": "chapter-0001",
                "expected_revision": 0,
                "plan_revision": 1,
                "plan_markdown": "# Chapter plan",
            },
        ),
    )
    registry.execute(
        context.__class__(**{**context.__dict__, "tool_call_id": "draft"}),
        _call(
            "draft",
            "write_chapter_draft",
            {
                "chapter_id": "chapter-0001",
                "expected_revision": 0,
                "plan_revision": 1,
                "draft_revision": 1,
                "mode": "write",
                "content": "The harbor bell rang once. Everyone faced the door.",
            },
        ),
    )

    submit = registry.execute(
        context.__class__(**{**context.__dict__, "tool_call_id": "submit"}),
        _call(
            "submit",
            "submit_chapter_candidate",
            {
                "chapter_id": "chapter-0001",
                "expected_revision": 0,
                "candidate_revision": 1,
                "plan_revision": 1,
                "draft_revision": 1,
                "summary": "The bell marks an arrival.",
                "observations": {
                    "events": [],
                    "character_changes": [],
                    "relationship_changes": [],
                    "world_fact_candidates": [],
                    "foreshadowing_candidates": [],
                    "requires_commit": True,
                },
                "state_patch": {
                    "operations": [
                        {
                            "op": "upsert",
                            "target_file": "canon/world_facts.json",
                            "target_id": "harbor-bell",
                            "expected_version": 1,
                            "value_fields": [
                                {"key": "heard", "json_value": "true"}
                            ],
                            "evidence_quotes": [
                                "“The harbor bell rang once.”",
                                "Everyone looked toward the entrance.",
                            ],
                            "rationale": "The draft establishes the bell.",
                        }
                    ],
                },
            },
        ),
    )

    assert submit.status == "error"
    assert submit.recoverable is True
    assert submit.error_code == "candidate_patch_evidence_not_verbatim"
    assert submit.content["rejected_evidence"] == [
        {"operation_index": 0, "evidence_indexes": [0, 1]}
    ]
    assert "retry:submit_chapter_candidate" in submit.allowed_actions
    assert not any(tmp_path.rglob("manifest.json"))


def test_targeted_chapter_edit_requires_a_unique_anchor(tmp_path: Path) -> None:
    registry = build_default_tool_registry()
    context = _context(
        tmp_path,
        role="chapter",
        scope_id="chapter-0001",
        phase="chapter",
        revision=0,
        call_id="plan",
    )
    registry.execute(
        context,
        _call(
            "plan",
            "plan_chapter_candidate",
            {
                "chapter_id": "chapter-0001",
                "expected_revision": 0,
                "plan_revision": 1,
                "plan_markdown": "计划",
            },
        ),
    )
    registry.execute(
        context.__class__(**{**context.__dict__, "tool_call_id": "draft"}),
        _call(
            "draft",
            "write_chapter_draft",
            {
                "chapter_id": "chapter-0001",
                "expected_revision": 0,
                "plan_revision": 1,
                "draft_revision": 1,
                "content": "重复。重复。",
            },
        ),
    )
    edit = registry.execute(
        context.__class__(**{**context.__dict__, "tool_call_id": "edit"}),
        _call(
            "edit",
            "edit_chapter_draft",
            {
                "chapter_id": "chapter-0001",
                "expected_revision": 0,
                "draft_revision": 1,
                "next_draft_revision": 2,
                "anchor": "重复",
                "replacement": "唯一",
            },
        ),
    )

    assert edit.status == "error"
    assert edit.error_code == "edit_anchor_not_unique"


def test_chapter_patch_evidence_repair_changes_only_rejected_quotes(
    tmp_path: Path,
) -> None:
    chapter_path = tmp_path / "chapters" / "chapter-0001"
    chapter_path.mkdir(parents=True)
    (chapter_path / "final.md").write_text(
        "The protagonist trusts companions after the trial.\n",
        encoding="utf-8",
    )
    write_json(
        chapter_path / "candidate_state_patch.json",
        {
            "schema_version": 1,
            "status": "candidate",
            "based_on": {
                "chapter_final": "chapters/chapter-0001/final.md",
                "observations": "chapters/chapter-0001/observations.json",
            },
            "operations": [
                {
                    "op": "upsert",
                    "target_file": "canon/characters.json",
                    "target_id": "protagonist",
                    "expected_version": 1,
                    "value": {"belief": "trusts companions"},
                    "evidence": [
                        {
                            "file": "chapters/chapter-0001/final.md",
                            "quote": "paraphrased trust",
                        }
                    ],
                    "rationale": "The chapter changes the protagonist's belief.",
                }
            ],
        },
    )
    write_json(
        chapter_path / "state_patch_rejection.json",
        {
            "schema": "passed",
            "versions": "passed",
            "evidence": "failed",
            "conflicts": "passed",
            "reasons": [
                "Operation 0 evidence 0 quote is not present in chapter_final."
            ],
        },
    )

    result = build_default_tool_registry().execute(
        _context(
            tmp_path,
            role="chapter",
            scope_id="chapter-0001",
            phase="state_patch_repair",
            revision=0,
            call_id="repair",
        ),
        _call(
            "repair",
            "submit_chapter_patch_evidence_repair",
            {
                "chapter_id": "chapter-0001",
                "expected_revision": 0,
                "repairs": [
                    {
                        "operation_index": 0,
                        "evidence_quotes": ["trusts companions"],
                    }
                ],
            },
        ),
    )

    assert result.status == "ok"
    assert result.terminal is True
    repaired = read_json(tmp_path / result.artifact_paths[0])
    assert repaired["operations"][0]["value"] == {"belief": "trusts companions"}
    assert repaired["operations"][0]["evidence"] == [
        {
            "file": "chapters/chapter-0001/final.md",
            "quote": "trusts companions",
        }
    ]


def _context(
    project_path: Path,
    *,
    role: str,
    phase: str,
    revision: int,
    call_id: str,
    scope_id: str | None = None,
    repair_contract: RepairContract | None = None,
    expected_candidate_revision: int | None = None,
) -> ToolExecutionContext:
    if role != "book" and scope_id is None:
        scope_id = "scope-1"
    return ToolExecutionContext(
        project_path=project_path,
        identity=AgentIdentity(
            project_id="project-1",
            role=role,  # type: ignore[arg-type]
            scope_id=scope_id,
        ),
        candidate_run_id="run-1",
        activation_id="activation-1",
        tool_call_id=call_id,
        phase=phase,
        expected_revision=revision,
        expected_candidate_revision=expected_candidate_revision,
        repair_contract=repair_contract,
    )


def _call(call_id: str, name: str, arguments: dict[str, object]) -> ToolCall:
    return ToolCall(
        id=call_id,
        name=name,
        arguments=arguments,
        raw_arguments="{}",
    )
