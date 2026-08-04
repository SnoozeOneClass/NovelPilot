from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError
from pydantic_ai import ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from app.agents.contracts import (
    ACTIVATION_TIMEOUT_MS,
    AgentRole,
    ArcChapterOutlineEntry,
    ArcPlanProposal,
    BookDiscussionContinue,
    BookDiscussionReady,
    BookDiscussionResult,
    BookDiscussionSuggestion,
    BookSupersededDecisionProposal,
    ChapterPlanProposal,
    CONNECT_TIMEOUT_MS,
    POOL_TIMEOUT_MS,
    READ_TIMEOUT_MS,
    WRITE_TIMEOUT_MS,
    ProfileCapabilities,
    ProfileSnapshot,
)
from app.agents.registry import (
    DEFAULT_EVALUATION_STRATEGY_REGISTRY,
    DEFAULT_TASK_REGISTRY,
)
from app.agents.roles import build_agent


def _profile() -> ProfileSnapshot:
    return ProfileSnapshot.create(
        profile_id="test-profile",
        display_name="Test Profile",
        api_family="openai_responses",
        base_url="https://provider.example/v1",
        model_id="opaque-model",
        capabilities=ProfileCapabilities(text_streaming=True, native_json_schema=True),
    )


def test_registry_freezes_s1_output_and_t1_without_domain_tools() -> None:
    profile = _profile()
    native = DEFAULT_TASK_REGISTRY.freeze_plan(
        task_id="task-native",
        project_id="project-a",
        run_id="run-a",
        task_key="book:evaluate:1",
        action_key="evaluate.book",
        role="evaluator",
        task_kind="evaluate.book",
        contract_version=1,
        book_id="book-a",
        canon_baseline_id="canon-a",
        semantic_goal="Evaluate the frozen candidate.",
        prompt="Evaluate this candidate.",
        context_manifest={"candidate_ref": "candidate-a"},
        profile_snapshot=profile,
        workspace_lock_version=1,
        workspace_work_cycle_id="book-evaluation-work-cycle",
    )
    prose = DEFAULT_TASK_REGISTRY.freeze_plan(
        task_id="task-prose",
        project_id="project-a",
        run_id="run-a",
        task_key="chapter:draft:1",
        action_key="chapter.draft",
        role="chapter_writer",
        task_kind="chapter.draft",
        contract_version=1,
        book_id="book-a",
        arc_id="arc-a",
        chapter_id="chapter-a",
        canon_baseline_id="canon-a",
        semantic_goal="Write the frozen chapter plan.",
        prompt="Write chapter prose only.",
        context_manifest={"plan_ref": "plan-a"},
        profile_snapshot=profile,
        book_baseline_id="book-baseline-a",
        arc_baseline_id="arc-baseline-a",
    )

    assert native.output_mode == "native_json_schema"
    assert native.required_capabilities == ("native_json_schema",)
    assert native.model_request_limit == 2
    assert prose.output_mode == "text_streaming"
    assert prose.required_capabilities == ("text_streaming",)
    assert prose.model_request_limit == 1
    assert native.toolset == prose.toolset == ()
    assert (
        native.connect_timeout_ms,
        native.pool_timeout_ms,
        native.write_timeout_ms,
        native.read_timeout_ms,
        native.activation_timeout_ms,
    ) == (
        CONNECT_TIMEOUT_MS,
        POOL_TIMEOUT_MS,
        WRITE_TIMEOUT_MS,
        READ_TIMEOUT_MS,
        ACTIVATION_TIMEOUT_MS,
    )


@pytest.mark.parametrize(
    ("role", "task_kind", "scope_kwargs"),
    (
        (
            "evaluator",
            "evaluate.book",
            {"book_id": "book-a", "arc_baseline_id": "arc-baseline-a"},
        ),
        (
            "evaluator",
            "evaluate.book",
            {"book_id": "book-a", "chapter_baseline_id": "chapter-baseline-a"},
        ),
        (
            "arc_planner",
            "arc.plan",
            {"book_id": "book-a", "arc_id": "arc-a"},
        ),
        (
            "arc_planner",
            "arc.plan",
            {
                "book_id": "book-a",
                "arc_id": "arc-a",
                "book_baseline_id": "book-baseline-a",
                "chapter_baseline_id": "chapter-baseline-a",
            },
        ),
        (
            "chapter_writer",
            "chapter.plan",
            {
                "book_id": "book-a",
                "arc_id": "arc-a",
                "chapter_id": "chapter-a",
                "arc_baseline_id": "arc-baseline-a",
            },
        ),
        (
            "chapter_writer",
            "chapter.plan",
            {
                "book_id": "book-a",
                "arc_id": "arc-a",
                "chapter_id": "chapter-a",
                "book_baseline_id": "book-baseline-a",
            },
        ),
    ),
    ids=(
        "book-with-arc-baseline",
        "book-with-chapter-baseline",
        "arc-without-book-baseline",
        "arc-with-chapter-baseline",
        "chapter-without-book-baseline",
        "chapter-without-arc-baseline",
    ),
)
def test_registry_rejects_baseline_ids_outside_the_owning_scope(
    role: AgentRole,
    task_kind: str,
    scope_kwargs: dict[str, str],
) -> None:
    with pytest.raises(ValidationError, match="Baseline IDs do not match scope_layer"):
        DEFAULT_TASK_REGISTRY.freeze_plan(
            task_id=f"invalid-{task_kind}",
            project_id="project-a",
            run_id="run-a",
            task_key=f"invalid:{task_kind}",
            action_key=task_kind,
            role=role,
            task_kind=task_kind,
            contract_version=1,
            canon_baseline_id="canon-a",
            semantic_goal="Exercise the frozen scope contract.",
            prompt="Return the requested semantic result.",
            context_manifest={"schema_id": "scope-contract-test"},
            profile_snapshot=_profile(),
            **scope_kwargs,
        )


def test_registry_rejects_book_parent_subject_on_another_task_kind() -> None:
    with pytest.raises(
        ValidationError,
        match="Only a Book parent-contract task may freeze an Arc review subject",
    ):
        DEFAULT_TASK_REGISTRY.freeze_plan(
            task_id="invalid-arc-parent-subject",
            project_id="project-a",
            run_id="run-a",
            task_key="invalid:arc-parent-subject",
            action_key="evaluate.arc_parent_contract",
            role="evaluator",
            task_kind="evaluate.arc_parent_contract",
            contract_version=1,
            book_id="book-a",
            arc_id="arc-a",
            book_baseline_id="book-baseline-a",
            arc_baseline_id="arc-baseline-a",
            subject_arc_baseline_id="arc-baseline-a",
            canon_baseline_id="canon-a",
            semantic_goal="Exercise the Book-parent-only subject contract.",
            prompt="Return the requested semantic result.",
            context_manifest={"schema_id": "subject-scope-contract-test"},
            profile_snapshot=_profile(),
        )


def test_role_agent_has_no_function_tools_and_no_shared_history() -> None:
    seen_message_counts: list[int] = []

    def response(messages: list[object], info: AgentInfo) -> ModelResponse:
        seen_message_counts.append(len(messages))
        assert info.model_request_parameters.function_tools == []
        assert info.model_request_parameters.output_mode == "native"
        return ModelResponse(
            parts=[
                TextPart(
                    '{"decision":"pass","summary":"All checks pass.","findings":[],'
                    '"requirement_coverage":[{"requirement_key":"ending_resolved",'
                    '"judgment":"aligned","rationale":"The responsible Arc reaches it."}]}'
                )
            ]
        )

    definition = DEFAULT_TASK_REGISTRY.get(
        role="evaluator",
        task_kind="evaluate.book",
        contract_version=1,
    )
    first = build_agent(model=FunctionModel(response), definition=definition)
    second = build_agent(model=FunctionModel(response), definition=definition)

    asyncio.run(first.run("First frozen candidate."))
    asyncio.run(second.run("Second frozen candidate."))

    assert seen_message_counts == [1, 1]


def test_book_discussion_control_shape_and_semantics_are_model_visible() -> None:
    definition = DEFAULT_TASK_REGISTRY.get(
        role="book_strategist",
        task_kind="book.discuss",
        contract_version=1,
    )
    properties = definition.output_schema["properties"]
    readiness = properties["readiness"]
    continue_schema = definition.output_schema["$defs"]["BookDiscussionContinue"]

    assert "question" not in properties
    assert "suggestions" not in properties
    assert readiness["discriminator"]["propertyName"] == "status"
    assert set(readiness["discriminator"]["mapping"]) == {"continue", "ready"}
    assert continue_schema["properties"]["suggestions"]["minItems"] == 2
    assert continue_schema["properties"]["suggestions"]["maxItems"] == 3
    assert (
        "Natural punctuation is allowed"
        in (continue_schema["properties"]["question"]["description"])
    )
    assert "punctuation is not a control protocol" in definition.task_instructions
    assert "Do not copy storage IDs" in definition.task_instructions

    model_facing_types = (
        BookDiscussionResult,
        BookDiscussionContinue,
        BookDiscussionReady,
        BookDiscussionSuggestion,
        BookSupersededDecisionProposal,
    )
    assert all(
        not model.__pydantic_decorators__.field_validators
        and not model.__pydantic_decorators__.model_validators
        for model in model_facing_types
    )


def test_cross_field_semantic_rules_are_present_in_model_visible_contracts() -> None:
    book_candidate = DEFAULT_TASK_REGISTRY.get(
        role="book_strategist",
        task_kind="book.synthesize",
        contract_version=1,
    )
    book_evaluation = DEFAULT_TASK_REGISTRY.get(
        role="evaluator",
        task_kind="evaluate.book",
        contract_version=1,
    )
    arc_evaluation = DEFAULT_TASK_REGISTRY.get(
        role="evaluator",
        task_kind="evaluate.arc",
        contract_version=1,
    )
    chapter_evaluation = DEFAULT_TASK_REGISTRY.get(
        role="evaluator",
        task_kind="evaluate.chapter",
        contract_version=1,
    )

    completion = book_candidate.output_schema["$defs"]["CompletionContract"]["properties"]
    assert set(completion) == {"completion_requirements"}
    assert "arc_topology" in book_candidate.output_schema["properties"]
    assert "advisory" in book_candidate.task_instructions
    assert "copying the exact requirement_key strings" in (book_candidate.task_instructions)
    arc_key_description = book_candidate.output_schema["$defs"]["BookArcContract"]["properties"][
        "completion_requirement_keys"
    ]["description"]
    assert "exactly match a requirement_key" in arc_key_description
    assert "semantically entail every indispensable named subject" in (
        book_candidate.task_instructions
    )
    assert "clarify roles" in book_candidate.task_instructions
    assert book_candidate.output_schema_version == 5

    book_properties = book_evaluation.output_schema["properties"]
    assert (
        "Exactly one semantic coverage judgment"
        in (book_properties["requirement_coverage"]["description"])
    )
    assert "local-repair" in book_evaluation.task_instructions
    assert "observed_components are diagnostic" in book_evaluation.task_instructions
    assert "complete same-layer Book candidate envelope" in (book_evaluation.task_instructions)
    assert "Book is the top creative authority" in book_evaluation.task_instructions
    assert "Coverage means the requirement necessarily follows" in (
        book_evaluation.task_instructions
    )
    assert "Candidate direction or constraints cannot substitute" in (
        book_evaluation.task_instructions
    )
    assert "Thematic compatibility or a broader category" in str(
        book_evaluation.output_schema["$defs"]["BookRequirementCoverageJudgment"]["properties"][
            "judgment"
        ]["description"]
    )
    assert book_evaluation.output_schema_version == 4
    assert book_evaluation.evaluation_strategy_version == 5
    assert book_evaluation.rubric_id == "book-candidate-rubric-v6"

    arc_properties = arc_evaluation.output_schema["properties"]
    assert "repair_scope" not in arc_properties
    assert "complete regeneration of the mutable-future chapter_outline" in (
        arc_evaluation.task_instructions
    )
    assert (
        "observed_components"
        not in (arc_evaluation.output_schema["$defs"]["ArcEvaluationIssue"]["properties"])
    )
    assert "escalate_to_book carries only" in arc_evaluation.task_instructions
    assert "required conclusion must be no stronger than the observable evidence" in (
        arc_evaluation.task_instructions
    )
    assert "active applied Arc guidance" in arc_evaluation.task_instructions
    assert "guidance request itself" in arc_evaluation.task_instructions
    assert "active_applied_guidance_present" in arc_evaluation.task_instructions
    assert "guidance_authority_judgment" in arc_evaluation.output_schema["required"]
    assert arc_evaluation.output_schema_version == 8
    assert arc_evaluation.evaluation_strategy_version == 10
    assert arc_evaluation.rubric_id == "arc-candidate-rubric-v9"
    assert arc_evaluation.context_policy_id == "arc-evaluator-context-v7"
    assert arc_evaluation.context_policy_version == 6

    chapter_properties = chapter_evaluation.output_schema["properties"]
    assert "escalation_target" not in chapter_properties
    assert "escalate_to_arc" in chapter_properties["decision"]["description"]
    assert "Never judge or route directly to Book" in chapter_evaluation.task_instructions
    assert "First distinguish candidate execution failure from plan failure" in (
        chapter_evaluation.task_instructions
    )
    assert "Do not repair an unsupported claim merely by weakening" in (
        chapter_evaluation.task_instructions
    )
    assert "new assignment-fulfillment issue" in chapter_evaluation.task_instructions
    assert "escalate_to_arc carries only" in chapter_evaluation.task_instructions
    assert "active applied Chapter guidance" in chapter_evaluation.task_instructions
    assert "guidance request itself" in chapter_evaluation.task_instructions
    assert "guidance_authority_judgment" in chapter_evaluation.output_schema["required"]


def test_local_repair_contracts_are_model_visible_and_arc_is_cohesive() -> None:
    definitions = {
        "book": DEFAULT_TASK_REGISTRY.get(
            role="book_strategist",
            task_kind="book.repair",
            contract_version=1,
        ),
        "arc": DEFAULT_TASK_REGISTRY.get(
            role="arc_planner",
            task_kind="arc.repair",
            contract_version=1,
        ),
        "chapter": DEFAULT_TASK_REGISTRY.get(
            role="chapter_writer",
            task_kind="chapter.repair.observation",
            contract_version=1,
        ),
    }

    expected_patch_components = {
        "book": {
            "direction",
            "constraints",
            "rolling_plan",
            "completion_contract",
            "arc_topology",
        },
        "chapter": {"observations", "canon"},
    }
    expected_versions = {"book": 6, "arc": 7, "chapter": 4}
    for layer in ("book", "chapter"):
        definition = definitions[layer]
        assert definition.output_schema_version == expected_versions[layer]
        assert set(definition.output_schema["properties"]) == {"changes"}
        changes = definition.output_schema["properties"]["changes"]
        assert changes["items"]["discriminator"]["propertyName"] == "component"
        assert (
            set(changes["items"]["discriminator"]["mapping"]) == (expected_patch_components[layer])
        )
        if layer == "chapter":
            assert "subset" in definition.task_instructions
        else:
            assert "every occurrence" in definition.task_instructions
        assert "Harness preserves" in definition.task_instructions

    arc_definition = definitions["arc"]
    assert arc_definition.output_schema_version == expected_versions["arc"]
    assert set(arc_definition.output_schema["properties"]) == {"chapter_outline"}
    assert arc_definition.repairable_components == ()
    assert "complete replacement" in str(
        arc_definition.output_schema["properties"]["chapter_outline"]["description"]
    )
    assert "complete chapter_outline" in arc_definition.task_instructions
    assert "mutable future interval" in arc_definition.task_instructions
    assert "partial entry patches" in arc_definition.task_instructions
    assert "Harness preserves" in arc_definition.task_instructions

    assert "unchanged replacement is rejected as a no-op" in (definitions["book"].task_instructions)

    book_schema_text = str(definitions["book"].output_schema)
    assert "selected_title" not in book_schema_text
    assert "semantically entail every" in definitions["book"].task_instructions
    assert "clarify roles" in definitions["book"].task_instructions

    chapter_observation = DEFAULT_TASK_REGISTRY.get(
        role="chapter_writer",
        task_kind="chapter.observe",
        contract_version=1,
    )
    evidence_description = chapter_observation.output_schema["$defs"]["SemanticCanonProposal"][
        "properties"
    ]["evidence_hint"]["description"]
    assert "do not copy an exact quote" in evidence_description
    assert "Harness owns subject upsert" in (chapter_observation.task_instructions)
    canon_properties = chapter_observation.output_schema["$defs"]["SemanticCanonProposal"][
        "properties"
    ]
    assert "operation" not in canon_properties
    assert "resolved" in canon_properties
    assert "established_facts" in chapter_observation.output_schema["properties"]
    assert "continuity_observations" not in chapter_observation.output_schema["properties"]
    assert chapter_observation.output_schema_version == 3

    chapter_evaluation = DEFAULT_TASK_REGISTRY.get(
        role="evaluator",
        task_kind="evaluate.chapter",
        contract_version=1,
    )
    chapter_issue_properties = chapter_evaluation.output_schema["$defs"]["ChapterEvaluationIssue"][
        "properties"
    ]
    assert set(chapter_issue_properties["observed_components"]["items"]["enum"]) == {
        "plan",
        "prose",
        "observations",
        "canon",
    }
    assert set(chapter_issue_properties["kind"]["enum"]) == {
        "explicit_conflict",
        "contract_unfulfilled",
        "unsupported_strong_conclusion",
        "derived_evidence_mismatch",
        "parent_authority_concern",
        "creator_owned_unknown",
    }
    assert "repair_scope" not in chapter_evaluation.output_schema["properties"]
    assert "recurrence" not in chapter_issue_properties
    assert "needs_user" not in (chapter_evaluation.output_schema["properties"]["decision"]["enum"])
    assert "absence of an explicit prior negation" in chapter_evaluation.task_instructions
    assert "history need not separately prove the non-recording" in (
        chapter_evaluation.task_instructions
    )
    assert "Harness derives" in chapter_evaluation.task_instructions
    assert chapter_evaluation.output_schema_version == 8
    assert chapter_evaluation.evaluation_strategy_version == 11
    assert chapter_evaluation.rubric_id == "chapter-candidate-rubric-v11"
    assert chapter_evaluation.context_policy_id == "chapter-evaluator-context-v6"
    assert chapter_evaluation.context_policy_version == 5

    chapter_repair_evaluation = DEFAULT_TASK_REGISTRY.get(
        role="evaluator",
        task_kind="verify_repair.chapter",
        contract_version=1,
    )
    chapter_repair_strategy = DEFAULT_EVALUATION_STRATEGY_REGISTRY.for_task("verify_repair.chapter")
    repair_issue_properties = chapter_repair_evaluation.output_schema["$defs"][
        "ChapterRepairVerificationIssue"
    ]["properties"]
    assert set(repair_issue_properties["recurrence"]["enum"]) == {
        "new",
        "persists_after_authorized_repair",
    }
    assert chapter_repair_evaluation.output_schema_version == 8
    assert chapter_repair_evaluation.evaluation_strategy_version == 13
    assert chapter_repair_evaluation.rubric_id == "chapter-repair-rubric-v13"
    assert chapter_repair_evaluation.context_policy_id == "chapter-repair-verification-context-v7"
    assert chapter_repair_evaluation.context_policy_version == 6
    assert set(chapter_repair_strategy.legal_semantic_signals) == {
        "pass",
        "local_repair",
        "escalate_to_arc",
    }
    assert "new assignment-fulfillment issue" in (chapter_repair_evaluation.task_instructions)
    assert "derived-evidence dependency closure" in (chapter_repair_evaluation.task_instructions)

    chapter_plan_repair = DEFAULT_TASK_REGISTRY.get(
        role="chapter_writer",
        task_kind="chapter.repair.plan",
        contract_version=1,
    )
    assert chapter_plan_repair.output_model is ChapterPlanProposal
    assert "complete replacement" in chapter_plan_repair.task_instructions
    assert "invalidates and regenerates" in chapter_plan_repair.task_instructions
    assert "Do not weaken or abandon the assignment" in (chapter_plan_repair.task_instructions)
    chapter_prose_repair = DEFAULT_TASK_REGISTRY.get(
        role="chapter_writer",
        task_kind="chapter.repair.prose",
        contract_version=1,
    )
    assert "instead of merely weakening" in chapter_prose_repair.task_instructions
    chapter_observation_repair = DEFAULT_TASK_REGISTRY.get(
        role="chapter_writer",
        task_kind="chapter.repair.observation",
        contract_version=1,
    )
    assert chapter_observation_repair.context_policy_id == "chapter-observation-repair-context-v4"
    assert chapter_observation_repair.context_policy_version == 4
    assert "derived dependency closure" in (chapter_observation_repair.task_instructions)

    arc_repair_evaluation = DEFAULT_TASK_REGISTRY.get(
        role="evaluator",
        task_kind="verify_repair.arc",
        contract_version=1,
    )
    assert arc_repair_evaluation.output_schema_version == 8
    assert arc_repair_evaluation.evaluation_strategy_version == 11
    assert arc_repair_evaluation.rubric_id == "arc-repair-rubric-v11"
    assert arc_repair_evaluation.context_policy_id == "arc-repair-verification-context-v6"
    assert arc_repair_evaluation.context_policy_version == 6

    book_repair_evaluation = DEFAULT_TASK_REGISTRY.get(
        role="evaluator",
        task_kind="verify_repair.book",
        contract_version=1,
    )
    assert book_repair_evaluation.output_schema_version == 5
    assert book_repair_evaluation.evaluation_strategy_version == 7
    assert book_repair_evaluation.rubric_id == "book-repair-rubric-v8"
    assert book_repair_evaluation.context_policy_id == "book-repair-verification-context-v4"
    assert book_repair_evaluation.context_policy_version == 4


def test_local_repair_patch_contracts_reject_empty_and_duplicate_changes() -> None:
    cases = (
        (
            "book_strategist",
            "book.repair",
            [
                {"component": "direction", "value": "First direction"},
                {"component": "direction", "value": "Second direction"},
            ],
        ),
        (
            "chapter_writer",
            "chapter.repair.observation",
            [
                {
                    "component": "observations",
                    "summary": "First summary",
                    "established_facts": [],
                },
                {
                    "component": "observations",
                    "summary": "Second summary",
                    "established_facts": [],
                },
            ],
        ),
    )
    for role, task_kind, duplicate_changes in cases:
        definition = DEFAULT_TASK_REGISTRY.get(
            role=role,
            task_kind=task_kind,
            contract_version=1,
        )
        assert definition.output_model is not None
        with pytest.raises(ValidationError):
            definition.output_model.model_validate({"changes": []})
        with pytest.raises(ValidationError, match="change each component"):
            definition.output_model.model_validate({"changes": duplicate_changes})

    arc_repair = DEFAULT_TASK_REGISTRY.get(
        role="arc_planner",
        task_kind="arc.repair",
        contract_version=1,
    )
    assert arc_repair.output_model.model_validate({"chapter_outline": []})
    with pytest.raises(ValidationError):
        arc_repair.output_model.model_validate(
            {
                "chapter_outline": [],
                "exit_conditions": ["Silently weaken the Book-owned outcome."],
            }
        )


def test_every_evaluator_task_freezes_one_complete_purpose_specific_strategy() -> None:
    evaluator_definitions = [
        definition for definition in DEFAULT_TASK_REGISTRY if definition.role == "evaluator"
    ]

    assert {definition.task_kind for definition in evaluator_definitions} == {
        strategy.task_kind for strategy in DEFAULT_EVALUATION_STRATEGY_REGISTRY
    }
    for definition in evaluator_definitions:
        strategy = DEFAULT_EVALUATION_STRATEGY_REGISTRY.for_task(definition.task_kind)
        assert definition.evaluation_strategy_id == strategy.strategy_id
        assert definition.evaluation_strategy_version == strategy.strategy_version
        assert definition.context_policy_id == strategy.context_policy_id
        assert definition.rubric_id == strategy.rubric_id
        assert definition.rubric_text == strategy.rubric_text
        assert strategy.rubric_text in definition.task_instructions
        assert strategy.context_includes
        assert strategy.context_excludes
        assert strategy.deterministic_prechecks
        assert strategy.legal_semantic_signals


def test_frozen_evaluator_plan_contains_concrete_rubric_and_strategy_identity() -> None:
    plan = DEFAULT_TASK_REGISTRY.freeze_plan(
        task_id="task-closure",
        project_id="project-a",
        run_id="run-a",
        task_key="arc:closure:1",
        action_key="evaluate.arc_closure:arc-a",
        role="evaluator",
        task_kind="evaluate.arc_closure",
        contract_version=1,
        book_id="book-a",
        arc_id="arc-a",
        workspace_lock_version=3,
        workspace_work_cycle_id="arc-closure-work-cycle",
        book_baseline_id="book-baseline-a",
        arc_baseline_id="arc-baseline-a",
        canon_baseline_id="canon-a",
        semantic_goal="Judge the frozen Arc closure boundary.",
        prompt="Evaluate the supplied semantic closure evidence.",
        context_manifest={"schema_id": "test-context"},
        profile_snapshot=_profile(),
        correction_lineage_id="lineage-a",
        correction_lineage_origin="review_initiated",
        automatic_correction_round=0,
    )

    assert plan.evaluation_strategy_id == "evaluate.arc_closure-strategy"
    assert plan.evaluation_strategy_version == 5
    assert plan.rubric_id == "arc-closure-rubric-v6"
    assert plan.rubric_text
    assert "Chapter count only triggered this review" in plan.rubric_text
    assert "exactly one discriminated outcome" in plan.rubric_text


def test_feedback_binding_distinguishes_guidance_from_correction_authority() -> None:
    common = {
        "project_id": "project-a",
        "run_id": "run-a",
        "role": "book_strategist",
        "task_kind": "book.discuss",
        "contract_version": 1,
        "book_id": "book-a",
        "canon_baseline_id": "canon-a",
        "semantic_goal": "Discuss the current Book workspace.",
        "prompt": "Continue the Book discussion.",
        "context_manifest": {"schema_id": "test-context"},
        "profile_snapshot": _profile(),
        "workspace_lock_version": 4,
        "workspace_work_cycle_id": "book-feedback-work-cycle",
    }

    guidance_plan = DEFAULT_TASK_REGISTRY.freeze_plan(
        task_id="task-guidance",
        task_key="book:discuss:guidance",
        action_key="book.discuss:guidance",
        source_feedback_id="feedback-guidance",
        **common,
    )
    assert guidance_plan.source_feedback_id == "feedback-guidance"
    assert guidance_plan.correction_lineage_id is None

    user_correction_plan = DEFAULT_TASK_REGISTRY.freeze_plan(
        task_id="task-user-correction",
        task_key="book:discuss:user-correction",
        action_key="book.discuss:user-correction",
        correction_lineage_id="lineage-user",
        correction_lineage_origin="user_initiated",
        automatic_correction_round=0,
        source_feedback_id="feedback-correction",
        **common,
    )
    assert user_correction_plan.source_feedback_id == "feedback-correction"
    assert user_correction_plan.correction_lineage_origin == "user_initiated"

    with pytest.raises(
        ValidationError,
        match="User-initiated correction must bind its feedback item",
    ):
        DEFAULT_TASK_REGISTRY.freeze_plan(
            task_id="task-user-correction-without-feedback",
            task_key="book:discuss:user-correction-without-feedback",
            action_key="book.discuss:user-correction-without-feedback",
            correction_lineage_id="lineage-user",
            correction_lineage_origin="user_initiated",
            automatic_correction_round=0,
            **common,
        )

    with pytest.raises(
        ValidationError,
        match="Review-initiated correction cannot bind user feedback",
    ):
        DEFAULT_TASK_REGISTRY.freeze_plan(
            task_id="task-review-correction-with-feedback",
            task_key="book:discuss:review-correction-with-feedback",
            action_key="book.discuss:review-correction-with-feedback",
            correction_lineage_id="lineage-review",
            correction_lineage_origin="review_initiated",
            automatic_correction_round=0,
            source_feedback_id="feedback-guidance",
            **common,
        )


def test_parent_and_evidence_review_rubrics_expose_harness_route_shape() -> None:
    expected = {
        "evaluate.arc_parent_contract": (
            "book_review_required and a parent_authority_concern issue are atomic",
            "chapter_evidence_review_required and a derived_evidence_mismatch issue are atomic",
            6,
            6,
            "arc-parent-contract-rubric-v7",
        ),
        "evaluate.book_parent_contract": (
            "Book is the top authority",
            "arc_evidence_review_required and a derived_evidence_mismatch issue are atomic",
            5,
            5,
            "book-parent-contract-rubric-v5",
        ),
        "evaluate.arc_closure": (
            "Return exactly one discriminated outcome",
            "Use correct_chapter_evidence only for a derived Observation",
            5,
            5,
            "arc-closure-rubric-v6",
        ),
        "evaluate.book_completion": (
            "Book is the top authority",
            "never emit parent_authority_concern",
            3,
            3,
            "book-completion-rubric-v3",
        ),
        "verify_evidence.chapter": (
            "When all three checks pass, return no issues",
            "cannot create a creator wait",
            3,
            3,
            "chapter-evidence-correction-rubric-v3",
        ),
    }

    for task_kind, (
        first_rule,
        second_rule,
        strategy_version,
        output_version,
        rubric_id,
    ) in expected.items():
        definition = DEFAULT_TASK_REGISTRY.get(
            role="evaluator",
            task_kind=task_kind,
            contract_version=1,
        )
        assert first_rule in definition.task_instructions
        assert second_rule in definition.task_instructions
        assert definition.evaluation_strategy_version == strategy_version
        assert definition.output_schema_version == output_version
        assert definition.rubric_id == rubric_id

    for task_kind in (
        "evaluate.arc_parent_contract",
        "evaluate.book_parent_contract",
    ):
        definition = DEFAULT_TASK_REGISTRY.get(
            role="evaluator",
            task_kind=task_kind,
            contract_version=1,
        )
        assert "creator wait is a standalone outcome" in definition.task_instructions
        assert "creator_input_need" in definition.output_schema["properties"]

    closure_definition = DEFAULT_TASK_REGISTRY.get(
        role="evaluator",
        task_kind="evaluate.arc_closure",
        contract_version=1,
    )
    assert set(closure_definition.output_schema["properties"]) == {"outcome"}
    assert (
        closure_definition.output_schema["properties"]["outcome"]["discriminator"]["propertyName"]
        == "kind"
    )

    evidence_definition = DEFAULT_TASK_REGISTRY.get(
        role="evaluator",
        task_kind="verify_evidence.chapter",
        contract_version=1,
    )
    assert "creator_input_need" not in evidence_definition.output_schema["properties"]

    arc_parent = DEFAULT_TASK_REGISTRY.get(
        role="evaluator",
        task_kind="evaluate.arc_parent_contract",
        contract_version=1,
    )
    assert "source change request is the evaluation target" in (arc_parent.task_instructions)
    assert (
        "exact inbound Chapter-to-Arc concern"
        in (arc_parent.output_schema["properties"]["arc_contract_judgment"]["description"])
    )


def test_arc_contract_exposes_complete_semantic_chapter_outline() -> None:
    definition = DEFAULT_TASK_REGISTRY.get(
        role="arc_planner",
        task_kind="arc.plan",
        contract_version=1,
    )
    properties = definition.output_schema["properties"]

    assert set(properties) == {"title", "chapter_outline"}
    assert not {
        "minimum_cumulative_chapter_count",
        "recommended_closure_cumulative_chapter_count",
        "maximum_cumulative_chapter_count",
        "closure_cumulative_chapter_count",
        "purpose",
    } & set(properties)
    assert "beats" not in properties
    assert "target_chapter_count" not in properties
    assert "one complete ordered chapter_outline" in definition.task_instructions
    assert "Harness derives the checkpoint" in definition.task_instructions
    assert "schedule observable evidence strong enough" in definition.task_instructions
    outline_item = definition.output_schema["$defs"]["ArcChapterOutlineEntry"]
    assert set(outline_item["properties"]) == {
        "title",
        "core_event",
        "hook",
        "scenes",
    }
    arc_evaluation = DEFAULT_TASK_REGISTRY.get(
        role="evaluator",
        task_kind="evaluate.arc",
        contract_version=1,
    )
    assert "Arc Planner's sole authoritative execution plan" in (arc_evaluation.task_instructions)
    arc_revision = DEFAULT_TASK_REGISTRY.get(
        role="arc_planner",
        task_kind="arc.revise",
        contract_version=1,
    )
    arc_repair = DEFAULT_TASK_REGISTRY.get(
        role="arc_planner",
        task_kind="arc.repair",
        contract_version=1,
    )
    assert set(arc_revision.output_schema["properties"]) == {"chapter_outline"}
    assert set(arc_repair.output_schema["properties"]) == {"chapter_outline"}
    assert "title" not in arc_revision.output_schema["properties"]
    assert "title" not in arc_repair.output_schema["properties"]
    chapter_draft = DEFAULT_TASK_REGISTRY.get(
        role="chapter_writer",
        task_kind="chapter.draft",
        contract_version=1,
    )
    assert "do not consume its core event early" in (chapter_draft.task_instructions)
    with pytest.raises(ValidationError):
        ArcChapterOutlineEntry(
            title=" ",
            core_event="Advance the evidence.",
            hook="Hand off the unresolved trace.",
            scenes=["Inspect the trace."],
        )
    with pytest.raises(ValidationError):
        ArcChapterOutlineEntry(
            title="The Trace",
            core_event="Advance the evidence.",
            hook="Hand off the unresolved trace.",
            scenes=[" "],
        )
    with pytest.raises(ValidationError):
        ArcChapterOutlineEntry.model_validate(
            {
                "title": "The Trace",
                "core_event": "Advance the evidence.",
                "hook": "Hand off the unresolved trace.",
                "scenes": ["Inspect the trace."],
                "outline_index": 1,
            }
        )
    with pytest.raises(ValidationError, match="desired_state_transition"):
        ArcPlanProposal.model_validate(
            {
                "title": "The Trace",
                "chapter_outline": [
                    {
                        "title": "The First Trace",
                        "core_event": "Expose the physical trace.",
                        "hook": "Its source remains unresolved.",
                        "scenes": ["Recover the trace."],
                    }
                ],
                "desired_state_transition": "Restate the Book-owned Arc outcome.",
            }
        )
