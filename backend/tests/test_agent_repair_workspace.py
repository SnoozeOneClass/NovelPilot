from pathlib import Path
from typing import Any

from app.harness.agents.domain_tools import build_default_tool_registry
from app.harness.agents.models import (
    AgentIdentity,
    BookCandidateSnapshot,
    ChapterCandidateSnapshot,
    RepairContract,
    StoryArcCandidateSnapshot,
)
from app.harness.agents.registry import ToolExecutionContext
from app.harness.agents.rubrics import component_fingerprints
from app.llm.gateway import ToolCall
from app.storage.json_files import read_json, write_json


def test_story_arc_repair_preserves_unmodified_components(tmp_path: Path) -> None:
    source_path = tmp_path / "arcs" / "arc-001" / "agent" / "a" / "source" / "c"
    source_path.mkdir(parents=True)
    source_payload = {
        "expected_revision": 0,
        "intent": "create",
        "arc_id": "arc-001",
        "plan_markdown": "# Arc\n\nOriginal plan.",
        "target_chapter_count": 2,
        "change_summary": "Create the first arc.",
    }
    source_artifact = "arcs/arc-001/agent/a/source/c/story-arc.json"
    write_json(tmp_path / source_artifact, source_payload)
    source = StoryArcCandidateSnapshot(
        plan=source_payload["plan_markdown"],
        target_chapter_count=source_payload["target_chapter_count"],
        change_summary=source_payload["change_summary"],
    )
    context = _context(
        tmp_path,
        role="story_arc",
        scope_id="arc-001",
        phase="planning",
        contract=_contract(source_artifact, component_fingerprints(source), ["plan"]),
    )
    registry = build_default_tool_registry()

    changed = registry.execute(
        context,
        _call(
            "replace",
            "replace_candidate_text",
            {"component": "plan", "content": "# Arc\n\nRepaired plan."},
        ),
    )
    finalized = registry.execute(
        _next_context(context, "finalize"),
        _call(
            "finalize",
            "submit_candidate_repair",
            {"summary": "Repair the Arc plan."},
        ),
    )

    assert changed.status == "ok"
    assert finalized.status == "ok"
    assert finalized.terminal is True
    candidate = read_json(tmp_path / finalized.artifact_paths[0])
    assert candidate["plan_markdown"] == "# Arc\n\nRepaired plan."
    assert candidate["target_chapter_count"] == 2
    assert candidate["change_summary"] == "Create the first arc."
    assert read_json(tmp_path / source_artifact) == source_payload


def test_book_repair_cannot_delete_below_required_title_minimum(
    tmp_path: Path,
) -> None:
    source_artifact, source = _write_book_source(tmp_path)
    context = _context(
        tmp_path,
        role="book",
        scope_id=None,
        phase="direction",
        contract=_contract(
            source_artifact,
            component_fingerprints(source),
            ["recommended_titles"],
        ),
    )
    registry = build_default_tool_registry()
    opened = registry.execute(context, _call("open", "open_candidate_repair", {}))
    title_items = [
        item
        for item in opened.content["structured_items"]
        if item["component"] == "recommended_titles"
    ]

    first_delete = registry.execute(
        _next_context(context, "delete-1"),
        _call(
            "delete-1",
            "delete_candidate_repair_item",
            {"item_id": title_items[0]["item_id"]},
        ),
    )
    rejected_delete = registry.execute(
        _next_context(context, "delete-2"),
        _call(
            "delete-2",
            "delete_candidate_repair_item",
            {"item_id": title_items[1]["item_id"]},
        ),
    )
    finalized = registry.execute(
        _next_context(context, "finalize"),
        _call(
            "finalize",
            "submit_candidate_repair",
            {"summary": "Retain three structurally valid title references."},
        ),
    )

    assert first_delete.status == "ok"
    assert rejected_delete.status == "error"
    assert rejected_delete.error_code == "repair_collection_minimum_violation"
    assert rejected_delete.content["minimum_items"] == 3
    assert rejected_delete.recoverable is True
    assert finalized.status == "ok"
    candidate = read_json(tmp_path / finalized.artifact_paths[0])
    assert len(candidate["recommended_titles"]) == 3


def test_book_repair_finalization_projects_structural_errors_as_domain_failures(
    tmp_path: Path,
) -> None:
    source_artifact, source = _write_book_source(tmp_path)
    context = _context(
        tmp_path,
        role="book",
        scope_id=None,
        phase="direction",
        contract=_contract(
            source_artifact,
            component_fingerprints(source),
            ["recommended_titles"],
        ),
    )
    registry = build_default_tool_registry()
    opened = registry.execute(context, _call("open", "open_candidate_repair", {}))
    workspace_path = tmp_path / opened.artifact_paths[0]
    workspace = read_json(workspace_path)
    workspace["current_components"]["recommended_titles"] = workspace[
        "current_components"
    ]["recommended_titles"][:1]
    write_json(workspace_path, workspace)

    result = registry.execute(
        _next_context(context, "finalize-invalid"),
        _call(
            "finalize-invalid",
            "submit_candidate_repair",
            {"summary": "This corrupted workspace must fail safely."},
        ),
    )

    assert result.status == "error"
    assert result.error_code == "repair_workspace_candidate_invalid"
    assert result.recoverable is True
    assert result.content["candidate_kind"] == "book_direction"
    assert result.content["violations"] == [
        {"path": "recommended_titles", "type": "too_short"}
    ]


def test_chapter_state_patch_repair_preserves_observation_semantics(
    tmp_path: Path,
) -> None:
    source_artifact, source = _write_chapter_source(tmp_path)
    context = _context(
        tmp_path,
        role="chapter",
        scope_id="chapter-002",
        phase="chapter",
        contract=_contract(
            source_artifact,
            component_fingerprints(source),
            ["state_patch"],
        ),
    )
    registry = build_default_tool_registry()
    opened = registry.execute(
        context,
        _call("open", "open_candidate_repair", {}),
    )
    reopened = registry.execute(
        _next_context(context, "reopen"),
        _call("reopen", "open_candidate_repair", {}),
    )
    patch_item = next(
        item
        for item in opened.content["structured_items"]
        if item["component"] == "state_patch"
    )
    assert "expected_version" not in patch_item["value"]
    updated = registry.execute(
        _next_context(context, "update"),
        _call(
            "update",
            "update_state_patch_operation_repair",
            {
                "item_id": patch_item["item_id"],
                "operation": {
                    "op": "upsert",
                    "target_file": "canon/world_facts.json",
                    "target_id": "bell",
                    "value_fields": [{"key": "heard", "json_value": "true"}],
                    "evidence_quotes": ["The harbor bell rang once."],
                    "rationale": "The sentence directly establishes the audible bell.",
                },
            },
        ),
    )
    finalized = registry.execute(
        _next_context(context, "finalize"),
        _call(
            "finalize",
            "submit_candidate_repair",
            {"summary": "Repair only the canon operation evidence."},
        ),
    )

    assert opened.status == "ok"
    assert reopened.status == "ok"
    assert reopened.content["workspace_id"] == opened.content["workspace_id"]
    assert reopened.content["structured_items"] == opened.content["structured_items"]
    assert updated.status == "ok"
    assert finalized.status == "ok"
    candidate = read_json(tmp_path / finalized.artifact_paths[0])
    source_payload = read_json(tmp_path / source_artifact)
    expected_observations = {
        **source_payload["observations"],
        "based_on": str(Path(finalized.artifact_paths[0]).parent / "draft.md").replace(
            "\\", "/"
        ),
    }
    assert candidate["observations"] == expected_observations
    assert candidate["plan_revision"] == 1
    assert candidate["draft_revision"] == 1
    assert candidate["candidate_revision"] == 2
    operation = candidate["state_patch"]["operations"][0]
    assert operation["id"] == patch_item["item_id"]
    assert operation["expected_version"] == 1
    assert operation["evidence"][0]["quote"] == "The harbor bell rang once."
    assert source_payload["observations"]["events"][0]["evidence_quote"].endswith(
        "},{"
    )
    next_source = ChapterCandidateSnapshot(
        plan=(tmp_path / Path(finalized.artifact_paths[0]).parent / "plan.md").read_text(
            encoding="utf-8"
        ),
        draft=(tmp_path / Path(finalized.artifact_paths[0]).parent / "draft.md").read_text(
            encoding="utf-8"
        ),
        observations=candidate["observations"],
        state_patch=candidate["state_patch"],
    )
    next_contract = RepairContract(
        evaluation_id="evaluation-2",
        source_activation_id="repair-activation",
        source_candidate_artifact_id=finalized.artifact_paths[0],
        source_candidate_revision=2,
        next_candidate_revision=3,
        open_issue_ids=["issue-2"],
        repair_brief="Verify the same operation again.",
        allowed_components=["state_patch"],
        source_component_fingerprints=component_fingerprints(next_source),
    )
    next_context = context.__class__(
        **{
            **context.__dict__,
            "activation_id": "repair-activation-2",
            "tool_call_id": "open-next",
            "repair_contract": next_contract,
        }
    )
    opened_next = registry.execute(
        next_context,
        _call("open-next", "open_candidate_repair", {}),
    )
    next_patch_item = next(
        item
        for item in opened_next.content["structured_items"]
        if item["component"] == "state_patch"
    )
    assert next_patch_item["item_id"] == patch_item["item_id"]


def test_chapter_full_draft_repair_does_not_regenerate_derived_artifacts(
    tmp_path: Path,
) -> None:
    source_artifact, source = _write_chapter_source(tmp_path)
    context = _context(
        tmp_path,
        role="chapter",
        scope_id="chapter-002",
        phase="chapter",
        contract=_contract(
            source_artifact,
            component_fingerprints(source),
            ["draft"],
        ),
    )
    registry = build_default_tool_registry()
    replaced = registry.execute(
        context,
        _call(
            "replace",
            "replace_candidate_text",
            {
                "component": "draft",
                "content": (
                    "The harbor bell rang once. Everyone faced the door. "
                    "Mara checked the sealed clock before speaking."
                ),
            },
        ),
    )
    finalized = registry.execute(
        _next_context(context, "finalize"),
        _call(
            "finalize",
            "submit_candidate_repair",
            {"summary": "Clarify Mara's visible action in the complete draft."},
        ),
    )

    assert replaced.status == "ok"
    assert finalized.status == "ok"
    candidate = read_json(tmp_path / finalized.artifact_paths[0])
    source_payload = read_json(tmp_path / source_artifact)
    assert candidate["observations"] == {
        **source_payload["observations"],
        "based_on": str(Path(finalized.artifact_paths[0]).parent / "draft.md").replace(
            "\\", "/"
        ),
    }
    assert candidate["state_patch"] == source_payload["state_patch"]
    assert candidate["draft_revision"] == 2
    assert candidate["plan_revision"] == 1


def test_chapter_repair_rebinds_observation_provenance_to_candidate_draft(
    tmp_path: Path,
) -> None:
    source_artifact, source = _write_chapter_source(tmp_path)
    context = _context(
        tmp_path,
        role="chapter",
        scope_id="chapter-002",
        phase="chapter",
        contract=_contract(
            source_artifact,
            component_fingerprints(source),
            ["observations"],
        ),
    )
    registry = build_default_tool_registry()
    opened = registry.execute(
        context,
        _call("open", "open_candidate_repair", {}),
    )
    observation = next(
        item
        for item in opened.content["structured_items"]
        if item["component"] == "observations"
    )
    updated = registry.execute(
        _next_context(context, "update"),
        _call(
            "update",
            "update_chapter_observation_repair",
            {
                "item_id": observation["item_id"],
                "summary": "The bell audibly announces an arrival.",
                "evidence_quote": "The harbor bell rang once.",
            },
        ),
    )
    finalized = registry.execute(
        _next_context(context, "finalize"),
        _call(
            "finalize",
            "submit_candidate_repair",
            {"summary": "Clarify the observation without authoring file metadata."},
        ),
    )

    assert updated.status == "ok"
    assert finalized.status == "ok"
    manifest_path = tmp_path / finalized.artifact_paths[0]
    candidate = read_json(manifest_path)
    expected_draft = str(manifest_path.relative_to(tmp_path).parent / "draft.md").replace(
        "\\", "/"
    )
    assert candidate["observations"]["based_on"] == expected_draft
    assert read_json(manifest_path.parent / "obs.json")["based_on"] == expected_draft


def _write_book_source(
    project_path: Path,
) -> tuple[str, BookCandidateSnapshot]:
    artifact = "book/agent/a/source/c/book-direction.json"
    payload: dict[str, Any] = {
        "expected_revision": 0,
        "candidate_revision": 1,
        "direction_markdown": "# Direction\n\nA fixed direction.",
        "constraints": {},
        "confirmed_decision_coverage": [],
        "recommended_titles": [
            {"title": f"Title {index}", "rationale": f"Rationale {index}."}
            for index in range(1, 5)
        ],
        "rolling_plan_markdown": "# Rolling plan\n\nPlan the current arc only.",
    }
    write_json(project_path / artifact, payload)
    return artifact, BookCandidateSnapshot(
        direction=payload["direction_markdown"],
        constraints=payload["constraints"],
        confirmed_decision_coverage=payload["confirmed_decision_coverage"],
        recommended_titles=payload["recommended_titles"],
        rolling_plan=payload["rolling_plan_markdown"],
    )


def _write_chapter_source(
    project_path: Path,
) -> tuple[str, ChapterCandidateSnapshot]:
    root = project_path / "chapters" / "chapter-002" / "agent" / "a" / "source" / "c"
    root.mkdir(parents=True)
    plan = "# Chapter 2\n\nReveal the bell clue."
    draft = "The harbor bell rang once. Everyone faced the door."
    observations = {
        "schema_version": 1,
        "status": "candidate",
        "based_on": "chapters/chapter-002/agent/a/source/c/draft.md",
        "events": [
            {
                "summary": "The bell announces an arrival.",
                "evidence_quote": "The harbor bell rang once.},{",
            }
        ],
        "character_changes": [],
        "relationship_changes": [],
        "world_fact_candidates": [],
        "foreshadowing_candidates": [],
        "requires_commit": True,
    }
    state_patch = {
        "schema_version": 1,
        "status": "candidate",
        "based_on": {
            "chapter_final": "chapters/chapter-002/final.md",
            "observations": "chapters/chapter-002/observations.json",
        },
        "operations": [
            {
                "op": "upsert",
                "target_file": "canon/world_facts.json",
                "target_id": "bell",
                "expected_version": 1,
                "value": {"heard": True},
                "evidence": [
                    {
                        "file": "chapters/chapter-002/final.md",
                        "quote": "The harbor bell rang once.",
                    }
                ],
                "rationale": "The bell is audible in the chapter.",
            }
        ],
    }
    (root / "plan.md").write_text(plan + "\n", encoding="utf-8")
    (root / "draft.md").write_text(draft + "\n", encoding="utf-8")
    artifact = "chapters/chapter-002/agent/a/source/c/manifest.json"
    write_json(
        project_path / artifact,
        {
            "schema_version": 1,
            "status": "candidate",
            "chapter_id": "chapter-002",
            "expected_revision": 0,
            "candidate_revision": 1,
            "plan_revision": 1,
            "draft_revision": 1,
            "summary": "Chapter 2 candidate.",
            "observations": observations,
            "state_patch": state_patch,
            "canon_versions": {
                "canon/characters.json": 1,
                "canon/relationships.json": 1,
                "canon/world_facts.json": 1,
                "canon/foreshadowing.json": 1,
            },
            "plan_path": "chapters/chapter-002/agent/a/source/c/plan.md",
            "draft_path": "chapters/chapter-002/agent/a/source/c/draft.md",
            "observations_path": "chapters/chapter-002/agent/a/source/c/obs.json",
            "state_patch_path": "chapters/chapter-002/agent/a/source/c/patch.json",
            "promotable": False,
        },
    )
    return artifact, ChapterCandidateSnapshot(
        plan=plan + "\n",
        draft=draft + "\n",
        observations=observations,
        state_patch=state_patch,
    )


def _contract(
    artifact: str,
    fingerprints: dict,
    allowed_components: list,
) -> RepairContract:
    return RepairContract(
        evaluation_id="evaluation-1",
        source_activation_id="source",
        source_candidate_artifact_id=artifact,
        source_candidate_revision=1,
        next_candidate_revision=2,
        open_issue_ids=["issue-1"],
        repair_brief="Repair the authorized semantic artifact.",
        allowed_components=allowed_components,
        source_component_fingerprints=fingerprints,
    )


def _context(
    project_path: Path,
    *,
    role: str,
    scope_id: str | None,
    phase: str,
    contract: RepairContract,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        project_path=project_path,
        identity=AgentIdentity(
            project_id="project-1",
            role=role,  # type: ignore[arg-type]
            scope_id=scope_id,
        ),
        candidate_run_id="candidate-run-1",
        activation_id="repair-activation",
        tool_call_id="open",
        phase=phase,
        expected_revision=0,
        expected_candidate_revision=1 if role == "book" else None,
        repair_contract=contract,
    )


def _next_context(
    context: ToolExecutionContext,
    call_id: str,
) -> ToolExecutionContext:
    return context.__class__(**{**context.__dict__, "tool_call_id": call_id})


def _call(call_id: str, name: str, arguments: dict[str, object]) -> ToolCall:
    return ToolCall(
        id=call_id,
        name=name,
        arguments=arguments,
        raw_arguments="{}",
    )
