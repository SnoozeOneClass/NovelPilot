import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import ValidationError

from app.harness.agents.models import (
    BookCandidateSnapshot,
    CandidateComponentName,
    CandidateKind,
    CandidateSnapshot,
    ChapterCandidateSnapshot,
    RepairWorkspace,
    RepairWorkspaceItem,
    RepairWorkspaceMutation,
    RepairContract,
    StoryArcCandidateSnapshot,
)
from app.harness.agents.evidence_matching import resolve_verbatim_evidence_quote
from app.harness.agents.persistence import activation_relative, json_document
from app.harness.agents.registry import ToolExecutionContext, ToolHandlerError
from app.harness.agents.rubrics import changed_components, component_fingerprints
from app.storage.json_files import read_json


BookCollection = Literal[
    "constraints.must_avoid",
    "constraints.creative_freedoms",
    "constraints.open_decisions",
    "confirmed_decision_coverage",
    "recommended_titles",
]
ObservationCollection = Literal[
    "events",
    "character_changes",
    "relationship_changes",
    "world_fact_candidates",
    "foreshadowing_candidates",
]


def repair_workspace_relative(context: ToolExecutionContext) -> Path:
    return activation_relative(context.identity, context.activation_id) / "c" / "repair-workspace.json"


def ensure_repair_workspace(context: ToolExecutionContext) -> RepairWorkspace:
    contract = _required_contract(context)
    relative = repair_workspace_relative(context)
    payload = read_json(context.project_path / relative, default=None)
    if payload is not None:
        workspace = RepairWorkspace.model_validate(payload)
        _validate_workspace_identity(context, workspace)
        return workspace

    candidate_kind = _candidate_kind(context)
    source_payload, source = _read_source_candidate(
        context.project_path,
        candidate_kind,
        contract.source_candidate_artifact_id,
    )
    fingerprints = component_fingerprints(source)
    if fingerprints != contract.source_component_fingerprints:
        raise ToolHandlerError(
            "repair_source_stale",
            "The source candidate no longer matches the pending repair contract.",
            recoverable=False,
            artifact_paths=[contract.source_candidate_artifact_id],
        )
    workspace_id = contract.repair_workspace_id or _stable_id(
        "repair",
        context.candidate_run_id,
        contract.evaluation_id,
        contract.source_candidate_artifact_id,
    )
    components = cast(
        dict[CandidateComponentName, Any],
        deepcopy(source.model_dump(mode="json", exclude={"kind"})),
    )
    persisted_handles = _read_persisted_item_handles(
        context.project_path,
        contract.source_candidate_artifact_id,
    )
    return RepairWorkspace(
        workspace_id=workspace_id,
        identity=context.identity,
        candidate_run_id=context.candidate_run_id,
        candidate_kind=candidate_kind,
        evaluation_id=contract.evaluation_id,
        source_candidate_artifact_id=contract.source_candidate_artifact_id,
        source_candidate_revision=contract.source_candidate_revision,
        next_candidate_revision=contract.next_candidate_revision,
        source_component_fingerprints=fingerprints,
        current_components=components,
        source_payload=source_payload,
        item_handles=_build_item_handles(
            workspace_id,
            candidate_kind,
            components,
            persisted_handles=persisted_handles,
        ),
    )


def workspace_document(workspace: RepairWorkspace) -> str:
    return json_document(workspace.model_dump(mode="json"))


def workspace_public_view(workspace: RepairWorkspace) -> dict[str, Any]:
    structured_items = []
    for handle in workspace.item_handles:
        value = _item_value(workspace, handle)
        if handle.component == "state_patch" and isinstance(value, dict):
            value = deepcopy(value)
            value.pop("expected_version", None)
        structured_items.append(
            {
                "item_id": handle.item_id,
                "component": handle.component,
                "collection": handle.collection,
                "value": value,
            }
        )
    return {
        "workspace_id": workspace.workspace_id,
        "candidate_kind": workspace.candidate_kind,
        "source_candidate_revision": workspace.source_candidate_revision,
        "next_candidate_revision": workspace.next_candidate_revision,
        "structured_items": structured_items,
        "mutations": [item.model_dump(mode="json") for item in workspace.mutations],
    }


def replace_text_component(
    context: ToolExecutionContext,
    workspace: RepairWorkspace,
    *,
    component: CandidateComponentName,
    content: str,
) -> RepairWorkspace:
    allowed_by_kind: dict[CandidateKind, set[CandidateComponentName]] = {
        "book_direction": {"direction", "rolling_plan"},
        "story_arc": {"plan", "change_summary"},
        "chapter": {"plan", "draft"},
    }
    if component not in allowed_by_kind[workspace.candidate_kind]:
        raise ToolHandlerError(
            "repair_component_not_text",
            "The selected candidate component cannot be replaced as text.",
            recoverable=True,
            allowed_actions=["open_candidate_repair"],
        )
    return _replace_component(context, workspace, component, content.strip())


def edit_text_component(
    context: ToolExecutionContext,
    workspace: RepairWorkspace,
    *,
    component: CandidateComponentName,
    anchor: str,
    replacement: str,
) -> RepairWorkspace:
    if workspace.candidate_kind != "chapter" or component not in {"plan", "draft"}:
        raise ToolHandlerError(
            "repair_component_not_editable_text",
            "Exact-anchor editing is available only for Chapter plan or draft.",
            recoverable=True,
        )
    current = workspace.current_components.get(component)
    if not isinstance(current, str):
        raise ToolHandlerError(
            "repair_component_invalid",
            "The selected text component is structurally invalid.",
            recoverable=False,
        )
    occurrences = current.count(anchor)
    if occurrences != 1:
        raise ToolHandlerError(
            "edit_anchor_not_unique",
            f"Targeted edit anchor matched {occurrences} locations; exactly one is required.",
            recoverable=True,
            allowed_actions=["replace_candidate_text"],
        )
    return _replace_component(
        context,
        workspace,
        component,
        current.replace(anchor, replacement, 1),
    )


def set_story_arc_chapter_count(
    context: ToolExecutionContext,
    workspace: RepairWorkspace,
    count: int,
) -> RepairWorkspace:
    if workspace.candidate_kind != "story_arc":
        raise ToolHandlerError(
            "repair_component_wrong_candidate_kind",
            "Chapter count belongs only to a Story Arc candidate.",
            recoverable=False,
        )
    return _replace_component(context, workspace, "target_chapter_count", count)


def add_book_item(
    context: ToolExecutionContext,
    workspace: RepairWorkspace,
    *,
    collection: BookCollection,
    primary: str,
    secondary: str | None,
) -> tuple[RepairWorkspace, str]:
    if workspace.candidate_kind != "book_direction":
        raise ToolHandlerError(
            "repair_component_wrong_candidate_kind",
            "Book structured items belong only to a Book candidate.",
            recoverable=False,
        )
    component = _book_collection_component(collection)
    values = _collection_values(workspace, component, collection)
    if collection == "confirmed_decision_coverage":
        constraints = cast(dict[str, Any], workspace.current_components["constraints"])
        confirmed = constraints.get("confirmed", [])
        if primary not in confirmed:
            raise ToolHandlerError(
                "repair_book_authority_violation",
                "Coverage may be added only for an exact Harness-confirmed decision.",
                recoverable=True,
                allowed_actions=["open_candidate_repair"],
            )
    value = _book_item_value(collection, primary, secondary)
    item_id = _next_item_id(workspace, component, collection)
    before = _component_fingerprint(workspace, component)
    values.append(value)
    handles = [
        *workspace.item_handles,
        RepairWorkspaceItem(
            item_id=item_id,
            component=component,
            collection=collection,
            index=len(values) - 1,
        ),
    ]
    revised = workspace.model_copy(update={"item_handles": handles})
    return _append_mutation(context, revised, "add", component, item_id, before), item_id


def add_chapter_observation(
    context: ToolExecutionContext,
    workspace: RepairWorkspace,
    *,
    collection: ObservationCollection,
    summary: str,
    evidence_quote: str,
) -> tuple[RepairWorkspace, str]:
    if workspace.candidate_kind != "chapter":
        raise ToolHandlerError(
            "repair_component_wrong_candidate_kind",
            "Observations belong only to a Chapter candidate.",
            recoverable=False,
        )
    values = _collection_values(workspace, "observations", collection)
    item_id = _next_item_id(workspace, "observations", collection)
    before = _component_fingerprint(workspace, "observations")
    values.append(
        {"id": item_id, "summary": summary.strip(), "evidence_quote": evidence_quote}
    )
    revised = workspace.model_copy(
        update={
            "item_handles": [
                *workspace.item_handles,
                RepairWorkspaceItem(
                    item_id=item_id,
                    component="observations",
                    collection=collection,
                    index=len(values) - 1,
                ),
            ]
        }
    )
    return (
        _append_mutation(context, revised, "add", "observations", item_id, before),
        item_id,
    )


def add_state_patch_operation(
    context: ToolExecutionContext,
    workspace: RepairWorkspace,
    *,
    operation: dict[str, Any],
) -> tuple[RepairWorkspace, str]:
    if workspace.candidate_kind != "chapter":
        raise ToolHandlerError(
            "repair_component_wrong_candidate_kind",
            "State patch operations belong only to a Chapter candidate.",
            recoverable=False,
        )
    values = _collection_values(workspace, "state_patch", "operations")
    item_id = _next_item_id(workspace, "state_patch", "operations")
    before = _component_fingerprint(workspace, "state_patch")
    values.append({"id": item_id, **operation})
    revised = workspace.model_copy(
        update={
            "item_handles": [
                *workspace.item_handles,
                RepairWorkspaceItem(
                    item_id=item_id,
                    component="state_patch",
                    collection="operations",
                    index=len(values) - 1,
                ),
            ]
        }
    )
    return (
        _append_mutation(context, revised, "add", "state_patch", item_id, before),
        item_id,
    )


def update_structured_item(
    context: ToolExecutionContext,
    workspace: RepairWorkspace,
    *,
    item_id: str,
    value: Any,
) -> RepairWorkspace:
    handle = _required_handle(workspace, item_id)
    _require_component_allowed(context, handle.component)
    before = _component_fingerprint(workspace, handle.component)
    values = _collection_values(workspace, handle.component, handle.collection)
    if handle.component in {"observations", "state_patch"} and isinstance(value, dict):
        value = {"id": item_id, **value}
    values[handle.index] = value
    return _append_mutation(
        context,
        workspace,
        "update",
        handle.component,
        item_id,
        before,
    )


def book_item_update_value(
    workspace: RepairWorkspace,
    *,
    item_id: str,
    primary: str,
    secondary: str | None,
) -> Any:
    handle = _required_handle(workspace, item_id)
    if workspace.candidate_kind != "book_direction" or handle.component not in {
        "constraints",
        "confirmed_decision_coverage",
        "recommended_titles",
    }:
        raise ToolHandlerError(
            "repair_item_wrong_candidate_kind",
            "The stable item ID is not a mutable Book item.",
            recoverable=True,
            allowed_actions=["open_candidate_repair"],
        )
    collection = cast(BookCollection, handle.collection)
    if collection == "confirmed_decision_coverage":
        current = _item_value(workspace, handle)
        decision = current.get("decision") if isinstance(current, dict) else None
        if not isinstance(decision, str) or primary != decision:
            raise ToolHandlerError(
                "repair_book_authority_violation",
                "A coverage repair cannot replace its Harness-confirmed decision text.",
                recoverable=True,
                content={"required_primary": decision},
                allowed_actions=["open_candidate_repair"],
            )
    return _book_item_value(collection, primary, secondary)


def delete_structured_item(
    context: ToolExecutionContext,
    workspace: RepairWorkspace,
    *,
    item_id: str,
) -> RepairWorkspace:
    handle = _required_handle(workspace, item_id)
    _require_component_allowed(context, handle.component)
    before = _component_fingerprint(workspace, handle.component)
    values = _collection_values(workspace, handle.component, handle.collection)
    minimum = _collection_minimum(handle.component, handle.collection)
    if minimum is not None and len(values) <= minimum:
        raise ToolHandlerError(
            "repair_collection_minimum_violation",
            (
                f"The {handle.component}.{handle.collection} collection must retain at least "
                f"{minimum} items in every complete candidate."
            ),
            recoverable=True,
            content={
                "component": handle.component,
                "collection": handle.collection,
                "current_items": len(values),
                "minimum_items": minimum,
            },
            allowed_actions=[
                "add_book_repair_item",
                "update_book_repair_item",
                "submit_candidate_repair",
            ],
        )
    del values[handle.index]
    handles: list[RepairWorkspaceItem] = []
    for item in workspace.item_handles:
        if item.item_id == item_id:
            continue
        if (
            item.component == handle.component
            and item.collection == handle.collection
            and item.index > handle.index
        ):
            item = item.model_copy(update={"index": item.index - 1})
        handles.append(item)
    revised = workspace.model_copy(update={"item_handles": handles})
    return _append_mutation(
        context,
        revised,
        "delete",
        handle.component,
        item_id,
        before,
    )


def finalize_repair_workspace(
    context: ToolExecutionContext,
    workspace: RepairWorkspace,
    *,
    summary: str,
) -> tuple[RepairWorkspace, dict[str, str | bytes], str, list[str]]:
    contract = _required_contract(context)
    _validate_workspace_identity(context, workspace)
    if workspace.status != "open":
        raise ToolHandlerError(
            "repair_workspace_already_finalized",
            "This repair workspace already produced its immutable candidate.",
            recoverable=False,
            artifact_paths=[workspace.final_candidate_artifact_id or ""],
        )
    _, source = _read_source_candidate(
        context.project_path,
        workspace.candidate_kind,
        workspace.source_candidate_artifact_id,
    )
    if component_fingerprints(source) != contract.source_component_fingerprints:
        raise ToolHandlerError(
            "repair_source_stale",
            "The source candidate changed before repair finalization.",
            recoverable=False,
        )
    if workspace.candidate_kind == "chapter":
        workspace = _bind_chapter_canon_versions(workspace)
    try:
        candidate = _snapshot_from_components(workspace)
    except ValidationError as exc:
        raise ToolHandlerError(
            "repair_workspace_candidate_invalid",
            (
                "The repaired workspace violates structural invariants of a complete "
                f"{workspace.candidate_kind} candidate. Correct the affected component before "
                "finalization."
            ),
            recoverable=True,
            content={
                "candidate_kind": workspace.candidate_kind,
                "violations": [
                    {
                        "path": ".".join(str(item) for item in error["loc"]),
                        "type": error["type"],
                    }
                    for error in exc.errors(include_input=False, include_url=False)
                ],
            },
            allowed_actions=["open_candidate_repair"],
        ) from exc
    changed = changed_components(
        contract.source_component_fingerprints,
        component_fingerprints(candidate),
    )
    if not changed:
        raise ToolHandlerError(
            "repair_workspace_no_changes",
            "Repair finalization requires at least one semantic candidate change.",
            recoverable=True,
            allowed_actions=["open_candidate_repair"],
        )
    unexpected = sorted(set(changed) - set(contract.allowed_components))
    if unexpected:
        raise ToolHandlerError(
            "candidate_repair_scope_violation",
            "Repair workspace changed components outside the Evaluator-authorized scope.",
            recoverable=True,
            content={
                "changed_components": changed,
                "allowed_components": list(contract.allowed_components),
                "unexpected_components": unexpected,
            },
        )
    files, candidate_path, artifact_paths = _candidate_files(
        context,
        workspace,
        candidate,
        changed,
        summary,
    )
    finalized = workspace.model_copy(
        update={
            "status": "finalized",
            "final_candidate_artifact_id": candidate_path,
        }
    )
    files[repair_workspace_relative(context).as_posix()] = workspace_document(finalized)
    return finalized, files, candidate_path, artifact_paths


def _bind_chapter_canon_versions(workspace: RepairWorkspace) -> RepairWorkspace:
    raw_versions = workspace.source_payload.get("canon_versions")
    if not isinstance(raw_versions, dict):
        raise ToolHandlerError(
            "repair_canon_version_snapshot_missing",
            "Chapter repair source has no Harness-owned canon version snapshot.",
            recoverable=False,
        )
    state_patch = workspace.current_components.get("state_patch")
    if not isinstance(state_patch, dict):
        raise ToolHandlerError(
            "repair_state_patch_invalid",
            "Repaired state patch has no valid operation list.",
            recoverable=True,
        )
    operations = state_patch.get("operations")
    if not isinstance(operations, list):
        raise ToolHandlerError(
            "repair_state_patch_invalid",
            "Repaired state patch has no valid operation list.",
            recoverable=True,
        )
    normalized_patch: dict[str, Any] = deepcopy(state_patch)
    normalized_operations = cast(list[Any], normalized_patch["operations"])
    for operation in normalized_operations:
        if not isinstance(operation, dict):
            raise ToolHandlerError(
                "repair_state_patch_invalid",
                "Repaired state patch contains an invalid operation.",
                recoverable=True,
            )
        target_file = operation.get("target_file")
        version = raw_versions.get(target_file) if isinstance(target_file, str) else None
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ToolHandlerError(
                "repair_canon_version_snapshot_invalid",
                "Chapter repair source canon version snapshot is incomplete.",
                recoverable=False,
            )
        operation["expected_version"] = version
    components = deepcopy(workspace.current_components)
    components["state_patch"] = normalized_patch
    return workspace.model_copy(update={"current_components": components})


def _replace_component(
    context: ToolExecutionContext,
    workspace: RepairWorkspace,
    component: CandidateComponentName,
    value: Any,
) -> RepairWorkspace:
    _require_component_allowed(context, component)
    if isinstance(value, str) and not value.strip():
        raise ToolHandlerError(
            "repair_component_blank",
            "A repaired text component cannot be blank.",
            recoverable=True,
        )
    before = _component_fingerprint(workspace, component)
    workspace.current_components[component] = value
    return _append_mutation(context, workspace, "replace", component, None, before)


def _collection_minimum(
    component: CandidateComponentName,
    collection: str,
) -> int | None:
    if component == "recommended_titles" and collection == "recommended_titles":
        return 3
    return None


def _append_mutation(
    context: ToolExecutionContext,
    workspace: RepairWorkspace,
    operation: Literal["replace", "add", "update", "delete"],
    component: CandidateComponentName,
    item_id: str | None,
    before: str,
) -> RepairWorkspace:
    _require_component_allowed(context, component)
    after = _component_fingerprint(workspace, component)
    if before == after:
        raise ToolHandlerError(
            "repair_mutation_no_change",
            "The requested repair mutation does not change the candidate.",
            recoverable=True,
        )
    mutation = RepairWorkspaceMutation(
        sequence=len(workspace.mutations) + 1,
        operation=operation,
        component=component,
        item_id=item_id,
        before_fingerprint=before,
        after_fingerprint=after,
        tool_call_id=context.tool_call_id,
    )
    return workspace.model_copy(update={"mutations": [*workspace.mutations, mutation]})


def _candidate_files(
    context: ToolExecutionContext,
    workspace: RepairWorkspace,
    candidate: CandidateSnapshot,
    changed: list[CandidateComponentName],
    summary: str,
) -> tuple[dict[str, str | bytes], str, list[str]]:
    root = activation_relative(context.identity, context.activation_id) / "c"
    payload = deepcopy(workspace.source_payload)
    components = candidate.model_dump(mode="json", exclude={"kind"})
    if workspace.candidate_kind == "book_direction":
        payload.update(
            {
                "direction_markdown": components["direction"],
                "constraints": components["constraints"],
                "confirmed_decision_coverage": components[
                    "confirmed_decision_coverage"
                ],
                "recommended_titles": components["recommended_titles"],
                "rolling_plan_markdown": components["rolling_plan"],
            }
        )
        candidate_path = (root / "book-direction.json").as_posix()
        item_ids_path = (root / "repair-item-ids.json").as_posix()
        return (
            {
                candidate_path: json_document(payload),
                item_ids_path: json_document(
                    [item.model_dump(mode="json") for item in workspace.item_handles]
                ),
            },
            candidate_path,
            [candidate_path, item_ids_path],
        )
    if workspace.candidate_kind == "story_arc":
        payload.update(
            {
                "plan_markdown": components["plan"],
                "target_chapter_count": components["target_chapter_count"],
                "change_summary": components["change_summary"],
            }
        )
        candidate_path = (root / "story-arc.json").as_posix()
        item_ids_path = (root / "repair-item-ids.json").as_posix()
        return (
            {
                candidate_path: json_document(payload),
                item_ids_path: json_document(
                    [item.model_dump(mode="json") for item in workspace.item_handles]
                ),
            },
            candidate_path,
            [candidate_path, item_ids_path],
        )

    plan_path = root / "plan.md"
    draft_path = root / "draft.md"
    observations_path = root / "obs.json"
    patch_path = root / "patch.json"
    manifest_path = root / "manifest.json"
    state_path = root / "workspace.json"
    item_ids_file = root / "repair-item-ids.json"
    plan = cast(str, components["plan"])
    draft = cast(str, components["draft"])
    observations = deepcopy(cast(dict[str, Any], components["observations"]))
    state_patch = cast(dict[str, Any], components["state_patch"])
    # Artifact locations are Harness-owned assembly metadata, not model-authored
    # semantics. Every immutable repaired candidate carries its own draft copy, so
    # keep the observation provenance bound to that copy even when the observation
    # items themselves were preserved from the source candidate.
    observations["based_on"] = draft_path.as_posix()
    _validate_patch_evidence(draft, state_patch)
    plan_revision = int(payload.get("plan_revision", 1)) + int("plan" in changed)
    draft_revision = int(payload.get("draft_revision", 1)) + int("draft" in changed)
    payload.update(
        {
            "candidate_revision": workspace.next_candidate_revision,
            "plan_revision": plan_revision,
            "draft_revision": draft_revision,
            "summary": summary,
            "observations": observations,
            "state_patch": state_patch,
            "plan_path": plan_path.as_posix(),
            "draft_path": draft_path.as_posix(),
            "draft_sha256": sha256(draft.encode("utf-8")).hexdigest(),
            "observations_path": observations_path.as_posix(),
            "state_patch_path": patch_path.as_posix(),
            "promotable": False,
        }
    )
    state = {
        "schema_version": 1,
        "chapter_id": context.identity.scope_id,
        "expected_revision": context.expected_revision,
        "plan_revision": plan_revision,
        "draft_revision": draft_revision,
        "draft_sha256": sha256(draft.encode("utf-8")).hexdigest(),
    }
    files: dict[str, str | bytes] = {
        plan_path.as_posix(): plan.rstrip() + "\n",
        draft_path.as_posix(): draft.rstrip() + "\n",
        observations_path.as_posix(): json_document(observations),
        patch_path.as_posix(): json_document(state_patch),
        manifest_path.as_posix(): json_document(payload),
        state_path.as_posix(): json_document(state),
        item_ids_file.as_posix(): json_document(
            [item.model_dump(mode="json") for item in workspace.item_handles]
        ),
    }
    artifacts = [
        manifest_path.as_posix(),
        plan_path.as_posix(),
        draft_path.as_posix(),
        observations_path.as_posix(),
        patch_path.as_posix(),
        item_ids_file.as_posix(),
    ]
    return files, manifest_path.as_posix(), artifacts


def _read_source_candidate(
    project_path: Path,
    candidate_kind: CandidateKind,
    artifact_id: str,
) -> tuple[dict[str, Any], CandidateSnapshot]:
    path = project_path / artifact_id
    payload = read_json(path, default=None)
    if not isinstance(payload, dict):
        raise ToolHandlerError(
            "repair_source_candidate_missing",
            "The pending repair source candidate is missing or invalid.",
            recoverable=False,
            artifact_paths=[artifact_id],
        )
    raw = cast(dict[str, Any], payload)
    try:
        if candidate_kind == "book_direction":
            return raw, BookCandidateSnapshot(
                direction=str(raw["direction_markdown"]),
                constraints=cast(dict[str, Any], raw["constraints"]),
                confirmed_decision_coverage=cast(
                    list[dict[str, Any]], raw["confirmed_decision_coverage"]
                ),
                recommended_titles=cast(
                    list[dict[str, Any]], raw["recommended_titles"]
                ),
                rolling_plan=str(raw["rolling_plan_markdown"]),
            )
        if candidate_kind == "story_arc":
            return raw, StoryArcCandidateSnapshot(
                plan=str(raw["plan_markdown"]),
                target_chapter_count=int(raw["target_chapter_count"]),
                change_summary=str(raw["change_summary"]),
            )
        root = Path(artifact_id).parent
        plan = _required_text(project_path / root / "plan.md")
        draft = _required_text(project_path / root / "draft.md")
        return raw, ChapterCandidateSnapshot(
            plan=plan,
            draft=draft,
            observations=cast(dict[str, Any], raw["observations"]),
            state_patch=cast(dict[str, Any], raw["state_patch"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ToolHandlerError(
            "repair_source_candidate_invalid",
            "The pending repair source candidate failed structural validation.",
            recoverable=False,
            artifact_paths=[artifact_id],
        ) from exc


def _snapshot_from_components(workspace: RepairWorkspace) -> CandidateSnapshot:
    c = workspace.current_components
    if workspace.candidate_kind == "book_direction":
        return BookCandidateSnapshot(
            direction=cast(str, c["direction"]),
            constraints=cast(dict[str, Any], c["constraints"]),
            confirmed_decision_coverage=cast(
                list[dict[str, Any]], c["confirmed_decision_coverage"]
            ),
            recommended_titles=cast(list[dict[str, Any]], c["recommended_titles"]),
            rolling_plan=cast(str, c["rolling_plan"]),
        )
    if workspace.candidate_kind == "story_arc":
        return StoryArcCandidateSnapshot(
            plan=cast(str, c["plan"]),
            target_chapter_count=cast(int, c["target_chapter_count"]),
            change_summary=cast(str, c["change_summary"]),
        )
    return ChapterCandidateSnapshot(
        plan=cast(str, c["plan"]),
        draft=cast(str, c["draft"]),
        observations=cast(dict[str, Any], c["observations"]),
        state_patch=cast(dict[str, Any], c["state_patch"]),
    )


def _build_item_handles(
    workspace_id: str,
    candidate_kind: CandidateKind,
    components: dict[CandidateComponentName, Any],
    *,
    persisted_handles: list[RepairWorkspaceItem] | None = None,
) -> list[RepairWorkspaceItem]:
    collections: list[tuple[CandidateComponentName, str, list[Any]]] = []
    if candidate_kind == "book_direction":
        constraints = cast(dict[str, Any], components["constraints"])
        for name in ("must_avoid", "creative_freedoms", "open_decisions"):
            collections.append(
                ("constraints", f"constraints.{name}", cast(list[Any], constraints.get(name, [])))
            )
        collections.extend(
            [
                (
                    "confirmed_decision_coverage",
                    "confirmed_decision_coverage",
                    cast(list[Any], components["confirmed_decision_coverage"]),
                ),
                (
                    "recommended_titles",
                    "recommended_titles",
                    cast(list[Any], components["recommended_titles"]),
                ),
            ]
        )
    elif candidate_kind == "chapter":
        observations = cast(dict[str, Any], components["observations"])
        for name in (
            "events",
            "character_changes",
            "relationship_changes",
            "world_fact_candidates",
            "foreshadowing_candidates",
        ):
            collections.append(
                ("observations", name, cast(list[Any], observations.get(name, [])))
            )
        patch = cast(dict[str, Any], components["state_patch"])
        collections.append(
            ("state_patch", "operations", cast(list[Any], patch.get("operations", [])))
        )
    handles: list[RepairWorkspaceItem] = []
    for component, collection, values in collections:
        for index, value in enumerate(values):
            existing_id = value.get("id") if isinstance(value, dict) else None
            item_id = (
                existing_id
                if isinstance(existing_id, str) and existing_id.strip()
                else _stable_id("item", workspace_id, component, collection, str(index))
            )
            persisted = next(
                (
                    item
                    for item in persisted_handles or []
                    if item.component == component
                    and item.collection == collection
                    and item.index == index
                ),
                None,
            )
            handles.append(
                RepairWorkspaceItem(
                    item_id=persisted.item_id if persisted is not None else item_id,
                    component=component,
                    collection=collection,
                    index=index,
                )
            )
    return handles


def _read_persisted_item_handles(
    project_path: Path,
    source_candidate_artifact_id: str,
) -> list[RepairWorkspaceItem]:
    relative = Path(source_candidate_artifact_id).parent / "repair-item-ids.json"
    payload = read_json(project_path / relative, default=None)
    if not isinstance(payload, list):
        return []
    try:
        handles = [RepairWorkspaceItem.model_validate(item) for item in payload]
    except (TypeError, ValueError):
        return []
    item_ids = [item.item_id for item in handles]
    return handles if len(item_ids) == len(set(item_ids)) else []


def _collection_values(
    workspace: RepairWorkspace,
    component: CandidateComponentName,
    collection: str,
) -> list[Any]:
    value = workspace.current_components.get(component)
    if component == "constraints":
        if not isinstance(value, dict) or not collection.startswith("constraints."):
            raise _invalid_collection()
        value = value.get(collection.split(".", 1)[1])
    elif component in {"observations", "state_patch"}:
        if not isinstance(value, dict):
            raise _invalid_collection()
        value = value.get(collection)
    if not isinstance(value, list):
        raise _invalid_collection()
    return value


def _invalid_collection() -> ToolHandlerError:
    return ToolHandlerError(
        "repair_collection_invalid",
        "The requested repair collection is missing or structurally invalid.",
        recoverable=False,
    )


def _required_handle(workspace: RepairWorkspace, item_id: str) -> RepairWorkspaceItem:
    handle = next((item for item in workspace.item_handles if item.item_id == item_id), None)
    if handle is None:
        raise ToolHandlerError(
            "repair_item_not_found",
            "The stable repair item ID is stale or does not exist.",
            recoverable=True,
            allowed_actions=["open_candidate_repair"],
        )
    return handle


def _item_value(workspace: RepairWorkspace, handle: RepairWorkspaceItem) -> Any:
    values = _collection_values(workspace, handle.component, handle.collection)
    if handle.index >= len(values):
        raise _invalid_collection()
    return values[handle.index]


def _next_item_id(
    workspace: RepairWorkspace,
    component: CandidateComponentName,
    collection: str,
) -> str:
    existing = {item.item_id for item in workspace.item_handles}
    ordinal = len(existing)
    while True:
        item_id = _stable_id(
            "item", workspace.workspace_id, component, collection, f"new-{ordinal}"
        )
        if item_id not in existing:
            return item_id
        ordinal += 1


def _book_collection_component(collection: BookCollection) -> CandidateComponentName:
    if collection.startswith("constraints."):
        return "constraints"
    return cast(CandidateComponentName, collection)


def _book_item_value(
    collection: BookCollection,
    primary: str,
    secondary: str | None,
) -> Any:
    if collection.startswith("constraints."):
        return primary.strip()
    if not secondary or not secondary.strip():
        raise ToolHandlerError(
            "repair_book_item_secondary_required",
            "Coverage and title items require both semantic text fields.",
            recoverable=True,
        )
    if collection == "confirmed_decision_coverage":
        return {"decision": primary.strip(), "candidate_evidence": secondary.strip()}
    return {"title": primary.strip(), "rationale": secondary.strip()}


def _required_contract(context: ToolExecutionContext) -> RepairContract:
    if context.repair_contract is None:
        raise ToolHandlerError(
            "repair_contract_missing",
            "Candidate repair Tools require a pending Harness repair contract.",
            recoverable=False,
        )
    return context.repair_contract


def _require_component_allowed(
    context: ToolExecutionContext,
    component: CandidateComponentName,
) -> None:
    contract = _required_contract(context)
    if component not in contract.allowed_components:
        raise ToolHandlerError(
            "candidate_repair_scope_violation",
            "The requested mutation is outside the Evaluator-authorized components.",
            recoverable=True,
            content={
                "requested_component": component,
                "allowed_components": list(contract.allowed_components),
            },
        )


def _candidate_kind(context: ToolExecutionContext) -> CandidateKind:
    if context.identity.role == "book":
        return "book_direction"
    return cast(CandidateKind, context.identity.role)


def _validate_workspace_identity(
    context: ToolExecutionContext,
    workspace: RepairWorkspace,
) -> None:
    contract = _required_contract(context)
    if (
        workspace.identity != context.identity
        or workspace.candidate_run_id != context.candidate_run_id
        or workspace.evaluation_id != contract.evaluation_id
        or workspace.source_candidate_artifact_id
        != contract.source_candidate_artifact_id
        or workspace.next_candidate_revision != contract.next_candidate_revision
    ):
        raise ToolHandlerError(
            "repair_workspace_identity_mismatch",
            "Repair workspace does not belong to this activation contract.",
            recoverable=False,
        )


def _component_fingerprint(
    workspace: RepairWorkspace,
    component: CandidateComponentName,
) -> str:
    return sha256(
        json.dumps(
            workspace.current_components[component],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _stable_id(prefix: str, *parts: str) -> str:
    digest = sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


def _required_text(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ToolHandlerError(
            "repair_source_candidate_unreadable",
            f"Repair source artifact could not be read: {path.name}",
            recoverable=False,
        ) from exc
    if not value.strip():
        raise ToolHandlerError(
            "repair_source_candidate_invalid",
            f"Repair source artifact is blank: {path.name}",
            recoverable=False,
        )
    return value


def _validate_patch_evidence(draft: str, state_patch: dict[str, Any]) -> None:
    operations = state_patch.get("operations")
    if not isinstance(operations, list):
        raise ToolHandlerError(
            "repair_state_patch_invalid",
            "Repaired state patch has no valid operation list.",
            recoverable=True,
        )
    rejected: list[dict[str, int]] = []
    for operation_index, operation in enumerate(operations):
        evidence = operation.get("evidence") if isinstance(operation, dict) else None
        if not isinstance(evidence, list):
            rejected.append({"operation_index": operation_index, "evidence_index": -1})
            continue
        for evidence_index, item in enumerate(evidence):
            quote = item.get("quote") if isinstance(item, dict) else None
            resolved_quote = (
                resolve_verbatim_evidence_quote(draft, quote)
                if isinstance(quote, str)
                else None
            )
            if resolved_quote is None:
                rejected.append(
                    {
                        "operation_index": operation_index,
                        "evidence_index": evidence_index,
                    }
                )
            else:
                item["quote"] = resolved_quote
    if rejected:
        raise ToolHandlerError(
            "candidate_patch_evidence_not_verbatim",
            "State-patch evidence must quote exact substrings from the repaired draft.",
            recoverable=True,
            content={"rejected_evidence": rejected},
        )
