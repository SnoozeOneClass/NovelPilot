from pathlib import Path

from app.harness.agents.domain_tools import build_default_tool_registry
from app.harness.agents.models import AgentIdentity
from app.harness.agents.registry import ToolExecutionContext
from app.llm.gateway import ToolCall
from app.storage.json_files import read_json, write_json


FORBIDDEN_PROVIDER_CONTROL_FIELDS = {
    "id",
    "chapter_id",
    "arc_id",
    "expected_revision",
    "candidate_revision",
    "plan_revision",
    "draft_revision",
    "next_draft_revision",
    "expected_version",
    "operation_index",
    "target_file",
    "target_id",
    "evidence_quote",
    "evidence_quotes",
    "candidate_locator",
    "evidence_locator",
    "committed_evidence_locator",
    "contract_revision",
    "workspace_id",
    "item_id",
    "collection",
    "secondary",
    "candidate_evidence",
    "op",
    "key",
    "json_value",
    "value_fields",
    "fingerprint",
    "max_characters",
    "start_character",
}


def test_registered_provider_tool_schemas_contain_no_harness_control_fields() -> None:
    registry = build_default_tool_registry()
    definitions = []
    for name in registry.registered_names():
        found = False
        for role in ("book", "story_arc", "chapter"):
            for phase in ("discussion", "direction", "planning", "revision", "chapter"):
                try:
                    resolved = registry.definitions(role=role, phase=phase, names=[name])
                except ValueError:
                    continue
                definitions.extend(resolved)
                found = True
                break
            if found:
                break
        assert found, f"No provider activation exposes registered Tool {name}."

    property_names: set[str] = set()

    def collect(value: object) -> None:
        if isinstance(value, dict):
            properties = value.get("properties")
            if isinstance(properties, dict):
                property_names.update(properties)
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    for definition in definitions:
        collect(definition.input_schema)

    assert not (property_names & FORBIDDEN_PROVIDER_CONTROL_FIELDS)
    assert "constraints.must_avoid" not in str(definitions)
    assert "constraints.creative_freedoms" not in str(definitions)
    assert "candidate_evidence" not in str(definitions)


def test_chapter_provider_schemas_are_closed_semantic_objects() -> None:
    schemas = build_default_tool_registry().definitions(
        role="chapter",
        phase="chapter",
        names=[
            "write_chapter_observations",
            "write_chapter_state_patch",
            "submit_chapter_candidate",
        ],
    )

    def assert_closed(value: object) -> None:
        if isinstance(value, dict):
            if value.get("type") == "object":
                assert value.get("additionalProperties") is False
            for child in value.values():
                assert_closed(child)
        elif isinstance(value, list):
            for child in value:
                assert_closed(child)

    for definition in schemas:
        assert_closed(definition.input_schema)


def test_book_direction_submission_binds_authority_and_revisions_in_harness(
    tmp_path: Path,
) -> None:
    arguments = {
        "direction_markdown": "# Direction\n\nTwo linked arcs expose one buried betrayal.",
        "constraints": {
            "must_avoid": ["No supernatural solution."],
            "creative_freedoms": ["The culprit may be chosen during arc planning."],
            "open_decisions": [],
        },
        "comparison_titles": [
            {"title": "The Second Tide", "rationale": "Highlights recurrence."},
            {"title": "Salt Ledger", "rationale": "Highlights the hidden record."},
        ],
        "rolling_plan_markdown": "Arc one exposes the clue; arc two resolves the betrayal.",
    }
    result = build_default_tool_registry().execute(
        _context(
            tmp_path,
            role="book",
            phase="direction",
            revision=4,
            call_id="book-direction",
            expected_candidate_revision=7,
            control_data={
                "confirmed_decisions": ["Use exactly two linked story arcs."],
                "selected_title": "Harbor of Echoes",
            },
        ),
        _call("book-direction", "submit_book_direction_candidate", arguments),
    )

    assert result.status == "ok"
    payload = read_json(tmp_path / result.artifact_paths[0])
    assert payload["expected_revision"] == 4
    assert payload["candidate_revision"] == 7
    assert payload["constraints"]["confirmed"] == [
        "Use exactly two linked story arcs."
    ]
    assert payload["constraints"]["must_preserve"] == [
        "Use exactly two linked story arcs."
    ]
    assert payload["recommended_titles"][0]["title"] == "Harbor of Echoes"


def test_same_story_arc_semantics_bind_to_different_internal_control_envelopes(
    tmp_path: Path,
) -> None:
    arguments = {
        "plan_markdown": "The team follows the false manifest to the flooded archive.",
        "target_chapter_count": 2,
        "change_summary": "Plan the next bounded arc.",
    }
    registry = build_default_tool_registry()
    first = registry.execute(
        _context(
            tmp_path / "first",
            role="story_arc",
            scope_id="arc-alpha",
            phase="planning",
            revision=1,
            call_id="first",
        ),
        _call("first", "submit_story_arc_candidate", arguments),
    )
    second = registry.execute(
        _context(
            tmp_path / "second",
            role="story_arc",
            scope_id="arc-beta",
            phase="revision",
            revision=8,
            call_id="second",
        ),
        _call("second", "submit_story_arc_candidate", arguments),
    )

    first_payload = read_json(tmp_path / "first" / first.artifact_paths[0])
    second_payload = read_json(tmp_path / "second" / second.artifact_paths[0])
    for key in ("plan_markdown", "target_chapter_count", "change_summary"):
        assert first_payload[key] == second_payload[key]
    assert (first_payload["arc_id"], first_payload["expected_revision"]) == (
        "arc-alpha",
        1,
    )
    assert (second_payload["arc_id"], second_payload["expected_revision"]) == (
        "arc-beta",
        8,
    )
    assert first_payload["intent"] == "create"
    assert second_payload["intent"] == "revise"


def test_book_discussion_assigns_suggestion_ids_and_preserves_prior_state(
    tmp_path: Path,
) -> None:
    arguments = {
        "reply": "The six-person cast still needs a boundary.",
        "direction_draft": "# Direction\n\nA fair closed-circle mystery.",
        "discussion_summary": "The user wants fair clues and a bounded cast.",
        "newly_confirmed_decisions": ["Use six principal suspects."],
        "superseded_decisions": [],
        "unresolved_questions": ["Which witness is outside the suspect group?"],
        "assumptions": [],
        "contradictions": [],
        "newly_selected_title": None,
        "question": "Which witness should remain outside the suspect group",
        "suggestions": [
                {
                    "label": "Harbor master",
                    "message": "Keep the harbor master outside the group.",
                    "rationale": "Preserves an external information source.",
                    "recommended": True,
                    "formal_title": None,
                },
                {
                    "label": "Doctor",
                    "message": "Keep the doctor outside the group.",
                    "rationale": "Preserves medical evidence independence.",
                    "recommended": False,
                    "formal_title": None,
                },
        ],
        "readiness": {"status": "continue", "reason": "One boundary remains."},
    }
    result = build_default_tool_registry().execute(
        _context(
            tmp_path,
            role="book",
            phase="discussion",
            revision=3,
            call_id="discussion",
            control_data={
                "confirmed_decisions": ["All clues must be fair."],
                "superseded_decisions": [],
                "selected_title": None,
                "turn": 4,
            },
        ),
        _call("discussion", "submit_book_discussion_update", arguments),
    )

    assert result.status == "ok"
    payload = read_json(tmp_path / result.artifact_paths[0])
    assert payload["expected_revision"] == 3
    assert payload["confirmed_decisions"] == [
        "All clues must be fair.",
        "Use six principal suspects.",
    ]
    assert payload["question"].endswith("？")
    assert all(item["id"].startswith("suggestion-") for item in payload["suggestions"])
    assert all(item["action"] == "answer" for item in payload["suggestions"])
    assert all(item["value"] is None for item in payload["suggestions"])


def test_book_discussion_harness_replaces_semantic_prior_without_exact_copy(
    tmp_path: Path,
) -> None:
    result = build_default_tool_registry().execute(
        _context(
            tmp_path,
            role="book",
            phase="discussion",
            revision=5,
            call_id="discussion-replace-decision",
            control_data={
                "confirmed_decisions": [
                    "Every clue shown to the reader must remain fair.",
                    "The ending should remain hopeful.",
                ],
                "superseded_decisions": [
                    {
                        "turn": 2,
                        "decision": "Use a single viewpoint.",
                        "replacement": "Alternate between two viewpoints.",
                        "reason": "The user expanded the structure.",
                        "user_evidence": "Use both investigators.",
                    }
                ],
                "selected_title": None,
                "turn": 6,
            },
        ),
        _call(
            "discussion-replace-decision",
            "submit_book_discussion_update",
            {
                "reply": "The clue rule now allows motivated misdirection.",
                "direction_draft": "# Direction\n\nA fair mystery with motivated lies.",
                "discussion_summary": "The user relaxed the clue rule.",
                "newly_confirmed_decisions": [],
                "superseded_decisions": [
                    {
                        "prior_meaning": "the earlier fair-clue requirement",
                        "replacement": (
                            "Clues may mislead when character motive supports them."
                        ),
                        "reason": "The user explicitly relaxed the rule.",
                        "user_evidence": "Allow motivated misdirection.",
                    }
                ],
                "unresolved_questions": ["Which relationship bears the final cost?"],
                "assumptions": [],
                "contradictions": [],
                "newly_selected_title": None,
                "question": "Which relationship should bear the final cost",
                "suggestions": [
                    {
                        "label": "Mentor",
                        "message": "Let the mentor relationship bear the cost.",
                        "rationale": "It completes the trust arc.",
                        "recommended": True,
                        "formal_title": None,
                    },
                    {
                        "label": "Sibling",
                        "message": "Let the sibling relationship bear the cost.",
                        "rationale": "It makes the cost personal.",
                        "recommended": False,
                        "formal_title": None,
                    },
                ],
                "readiness": {"status": "continue", "reason": "One choice remains."},
            },
        ),
    )

    assert result.status == "ok"
    payload = read_json(tmp_path / result.artifact_paths[0])
    replacement = "Clues may mislead when character motive supports them."
    assert payload["confirmed_decisions"] == [
        replacement,
        "The ending should remain hopeful.",
    ]
    assert payload["superseded_decisions"] == [
        {
            "turn": 6,
            "decision": "Every clue shown to the reader must remain fair.",
            "replacement": replacement,
            "reason": "The user explicitly relaxed the rule.",
            "user_evidence": "Allow motivated misdirection.",
        }
    ]


def test_book_discussion_binds_title_action_without_question_text_matching(
    tmp_path: Path,
) -> None:
    arguments = {
        "reply": "The remaining decision is the formal title.",
        "direction_draft": "# Direction\n\nA fair closed-circle mystery.",
        "discussion_summary": "The story direction has converged.",
        "newly_confirmed_decisions": [],
        "superseded_decisions": [],
        "unresolved_questions": ["Choose the formal title."],
        "assumptions": [],
        "contradictions": [],
        "newly_selected_title": None,
        "question": "书名如果进入定稿，你更倾向下列哪一个作为正式书名",
        "suggestions": [
            {
                "label": "保留工作名",
                "message": "保留现有工作名作为正式书名。",
                "rationale": "It best matches the core mechanism.",
                "recommended": True,
                "formal_title": "退潮前的十一分钟",
            },
            {
                "label": "改用地点意象",
                "message": "改用更冷峻的地点意象书名。",
                "rationale": "It emphasizes the closed setting.",
                "recommended": False,
                "formal_title": "缺失的潮窗",
            },
        ],
        "readiness": {"status": "continue", "reason": "A title is still open."},
    }
    result = build_default_tool_registry().execute(
        _context(
            tmp_path,
            role="book",
            phase="discussion",
            revision=3,
            call_id="discussion-title-options",
            control_data={
                "confirmed_decisions": [],
                "superseded_decisions": [],
                "selected_title": None,
                "turn": 4,
            },
        ),
        _call(
            "discussion-title-options",
            "submit_book_discussion_update",
            arguments,
        ),
    )

    assert result.status == "ok"
    payload = read_json(tmp_path / result.artifact_paths[0])
    assert [item["action"] for item in payload["suggestions"]] == [
        "select_title",
        "select_title",
    ]
    assert [item["value"] for item in payload["suggestions"]] == [
        "退潮前的十一分钟",
        "缺失的潮窗",
    ]


def test_book_discussion_uses_harness_bound_title_over_model_repetition(
    tmp_path: Path,
) -> None:
    result = build_default_tool_registry().execute(
        _context(
            tmp_path,
            role="book",
            phase="discussion",
            revision=3,
            call_id="discussion-title",
            control_data={
                "confirmed_decisions": [],
                "superseded_decisions": [],
                "selected_title": "退潮前的十一分钟",
                "turn": 4,
            },
        ),
        _call(
            "discussion-title",
            "submit_book_discussion_update",
            {
                "reply": "The title decision is complete.",
                "direction_draft": "# Direction\n\nA fair closed-circle mystery.",
                "discussion_summary": "The direction and title are complete.",
                "newly_confirmed_decisions": [],
                "superseded_decisions": [],
                "unresolved_questions": [],
                "assumptions": [],
                "contradictions": [],
                "newly_selected_title": "模型不需要精确复述",
                "question": "",
                "suggestions": [],
                "readiness": {"status": "ready", "reason": "Ready for review."},
            },
        ),
    )

    assert result.status == "ok"
    payload = read_json(tmp_path / result.artifact_paths[0])
    assert payload["selected_title"] == "退潮前的十一分钟"
    assert payload["question"] is None


def test_book_discussion_converges_to_evaluation_after_ten_persisted_turns(
    tmp_path: Path,
) -> None:
    result = build_default_tool_registry().execute(
        _context(
            tmp_path,
            role="book",
            phase="discussion",
            revision=10,
                call_id="discussion-convergence-bound",
                control_data={
                    "confirmed_decisions": [
                        "Use first-person courtroom narration.",
                        "Use third-person courtroom narration.",
                    ],
                "superseded_decisions": [],
                "selected_title": "Harbor of Trust",
                "turn": 10,
            },
        ),
        _call(
            "discussion-convergence-bound",
            "submit_book_discussion_update",
            {
                "reply": "The remaining implementation detail can go to evaluation.",
                "direction_draft": "# Direction\n\nA fair harbor mystery.",
                "discussion_summary": "The Book contract is ready for evaluation.",
                "newly_confirmed_decisions": [],
                "superseded_decisions": [
                    {
                        "prior_meaning": "Use person-based courtroom narration.",
                        "replacement": "Open the courtroom sequence with evidence.",
                        "reason": "The user selected the evidence-first option.",
                        "user_evidence": "Open with evidence.",
                    }
                ],
                "unresolved_questions": ["Which exact courtroom beat comes first?"],
                "assumptions": [],
                "contradictions": [],
                "newly_selected_title": None,
                "question": "Which exact courtroom beat should come first",
                "suggestions": [
                    {
                        "label": "Evidence",
                        "message": "Open with evidence.",
                        "rationale": "It is direct.",
                        "recommended": True,
                        "formal_title": None,
                    },
                    {
                        "label": "Procedure",
                        "message": "Open with procedure.",
                        "rationale": "It is orderly.",
                        "recommended": False,
                        "formal_title": None,
                    },
                ],
                "readiness": {
                    "status": "continue",
                    "reason": "One local beat remains.",
                },
            },
        ),
    )

    assert result.status == "ok"
    payload = read_json(tmp_path / result.artifact_paths[0])
    assert payload["readiness"]["status"] == "ready"
    assert "ten persisted turns" in payload["readiness"]["reason"]
    assert payload["question"] is None
    assert payload["suggestions"] == []
    assert "Open the courtroom sequence with evidence." in payload[
        "confirmed_decisions"
    ]
    assert any(
        "could not be bound uniquely" in item
        for item in payload["contradictions"]
    )


def test_chapter_semantics_are_assembled_with_harness_ids_versions_and_evidence(
    tmp_path: Path,
) -> None:
    registry = build_default_tool_registry()
    base = _context(
        tmp_path,
        role="chapter",
        scope_id="chapter-0001",
        phase="chapter",
        revision=5,
        call_id="plan",
    )
    plan = registry.execute(
        base,
        _call(
            "plan",
            "plan_chapter_candidate",
            {"plan_markdown": "Reveal that the harbor bell marks a secret arrival."},
        ),
    )
    draft = registry.execute(
        _with_call(base, "draft"),
        _call(
            "draft",
            "write_chapter_draft",
            {
                "content": (
                    "The harbor bell rang once. The harbor bell rang twice. "
                    "Everyone faced the door."
                )
            },
        ),
    )
    inspect = registry.execute(
        _with_call(base, "inspect"),
        _call("inspect", "inspect_chapter_consistency", {}),
    )
    observations = registry.execute(
        _with_call(base, "observations"),
        _call(
            "observations",
            "write_chapter_observations",
            {
                "observations": {
                    "events": [
                        {"summary": "The harbor bell rang once."},
                        {"summary": "潜艇上浮。"},
                    ],
                    "character_changes": [],
                    "relationship_changes": [],
                    "world_fact_candidates": [],
                    "foreshadowing_candidates": [],
                }
            },
        ),
    )
    patch = registry.execute(
        _with_call(base, "patch"),
        _call(
            "patch",
            "write_chapter_state_patch",
            {
                "state_patch": {
                    "operations": [
                        {
                            "change_kind": "establish",
                            "entity_kind": "world_fact",
                            "entity_name": "harbor bell arrival signal",
                            "resulting_state": (
                                "The harbor bell signals a secret arrival."
                            ),
                            "evidence_hint": "The harbor bell rang once.",
                            "rationale": "The bell announces a secret arrival.",
                        }
                    ]
                }
            },
        ),
    )
    submit = registry.execute(
        _with_call(base, "submit"),
        _call(
            "submit",
            "submit_chapter_candidate",
            {"summary": "The bell reveals the arrival signal."},
        ),
    )

    assert all(item.status == "ok" for item in (plan, draft, inspect, observations, patch))
    assert submit.status == "ok"
    manifest_path = next(
        item for item in submit.artifact_paths if item.endswith("manifest.json")
    )
    manifest = read_json(tmp_path / manifest_path)
    operation = manifest["state_patch"]["operations"][0]
    assert manifest["chapter_id"] == "chapter-0001"
    assert manifest["expected_revision"] == 5
    assert manifest["plan_revision"] == 1
    assert manifest["draft_revision"] == 1
    assert manifest["observations"]["events"][0]["id"].startswith("item-")
    assert manifest["observations"]["events"][0]["evidence_quote"] in (
        tmp_path / submit.artifact_paths[2]
    ).read_text(encoding="utf-8")
    assert len(manifest["observations"]["events"]) == 1
    assert manifest["normalization"] == {
        "submitted_observation_count": 2,
        "retained_observation_count": 1,
        "dropped_unbound_observation_count": 1,
    }
    assert submit.content["dropped_unbound_observation_count"] == 1
    assert operation["target_file"] == "canon/world_facts.json"
    assert operation["target_id"].startswith("canon-")
    assert operation["expected_version"] == 1
    assert operation["evidence"][0]["quote"] in (
        tmp_path / submit.artifact_paths[2]
    ).read_text(encoding="utf-8")
    assert not (tmp_path / "chapters" / "chapter-0001" / "final.md").exists()


def test_removed_exact_control_tools_are_not_registered() -> None:
    names = build_default_tool_registry().registered_names()
    assert "edit_chapter_draft" not in names
    assert "edit_candidate_text" not in names
    assert "read_chapter_evidence" not in names
    assert "submit_chapter_patch_evidence_repair" not in names


def _context(
    project_path: Path,
    *,
    role: str,
    phase: str,
    revision: int,
    call_id: str,
    scope_id: str | None = None,
    expected_candidate_revision: int | None = None,
    control_data: dict[str, object] | None = None,
) -> ToolExecutionContext:
    if role != "book" and scope_id is None:
        scope_id = "scope-1"
    if role == "chapter":
        for relative in (
            "canon/characters.json",
            "canon/relationships.json",
            "canon/world_facts.json",
            "canon/foreshadowing.json",
        ):
            path = project_path / relative
            if not path.is_file():
                write_json(path, {"schema_version": 1, "version": 1, "items": {}})
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
        control_data=control_data or {},
    )


def _with_call(context: ToolExecutionContext, call_id: str) -> ToolExecutionContext:
    return context.__class__(**{**context.__dict__, "tool_call_id": call_id})


def _call(call_id: str, name: str, arguments: dict[str, object]) -> ToolCall:
    return ToolCall(
        id=call_id,
        name=name,
        arguments=arguments,
        raw_arguments="{}",
    )
