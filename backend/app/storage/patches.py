import json
from datetime import UTC, datetime
from pathlib import Path

from app.core.paths import ensure_relative_artifact_path
from app.schemas.patches import (
    CandidatePatchOperation,
    CandidateStatePatch,
    CommittedStatePatch,
    PatchValidationResult,
)
from app.schemas.state import VersionedState
from app.storage.json_files import read_json
from app.storage.text_files import read_text_file
from app.storage.transactions import commit_file_transaction


ALLOWED_CANON_TARGETS = {
    "canon/characters.json",
    "canon/relationships.json",
    "canon/world_facts.json",
    "canon/foreshadowing.json",
}
ALLOWED_PATCH_SOURCE_FILES = {
    "chapter_final": "final.md",
    "observations": "observations.json",
}
OBSERVATIONS_REQUIRED_STATUS = "candidate"


class PatchValidationError(ValueError):
    def __init__(self, result: PatchValidationResult) -> None:
        self.result = result
        super().__init__("Candidate state patch failed validation.")


def read_canon_versions(project_path: Path) -> dict[str, int]:
    """Capture the concurrency versions owned by one Chapter activation."""

    return {
        target_file: _read_versioned_state(project_path / target_file).version
        for target_file in sorted(ALLOWED_CANON_TARGETS)
    }


def validate_candidate_state_patch(
    project_path: Path,
    patch: CandidateStatePatch,
) -> PatchValidationResult:
    schema_reasons = _validate_schema_boundaries(patch)
    version_reasons = _validate_versions(project_path, patch)
    evidence_reasons = _validate_evidence(project_path, patch)
    conflict_reasons = _validate_conflicts(project_path, patch)

    reasons = schema_reasons + version_reasons + evidence_reasons + conflict_reasons
    return PatchValidationResult(
        schema="failed" if schema_reasons else "passed",
        versions="failed" if version_reasons else "passed",
        evidence="failed" if evidence_reasons else "passed",
        conflicts="failed" if conflict_reasons else "passed",
        reasons=reasons,
    )


def commit_candidate_state_patch(
    project_path: Path,
    patch: CandidateStatePatch,
    committed_patch_path: Path,
) -> CommittedStatePatch:
    validation = validate_candidate_state_patch(project_path, patch)
    if validation.reasons:
        raise PatchValidationError(validation)

    states = _load_target_states(project_path, patch.operations)
    for operation in patch.operations:
        target_file = _normalize_project_relative_path(operation.target_file)
        state = states[target_file]
        _apply_operation(state, operation)

    committed = CommittedStatePatch(
        committed_at=datetime.now(UTC).isoformat(),
        operations=patch.operations,
        validation=validation,
    )
    files: dict[str, str | bytes] = {}
    for target_file, state in states.items():
        state.version += 1
        files[target_file] = _json_document(state.model_dump(mode="json"))
    try:
        committed_relative = committed_patch_path.resolve().relative_to(
            project_path.resolve()
        )
    except ValueError as exc:
        raise ValueError("Committed state patch path escapes the project.") from exc
    files[committed_relative.as_posix()] = _json_document(
        committed.model_dump(mode="json", by_alias=True)
    )
    commit_file_transaction(
        project_path,
        kind=f"state-patch-{committed_relative.parent.name}",
        files=files,
    )
    return committed


def _json_document(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _validate_schema_boundaries(patch: CandidateStatePatch) -> list[str]:
    reasons: list[str] = []
    if patch.schema_version != 1:
        reasons.append(f"Unsupported patch schema_version: {patch.schema_version}.")

    for index, operation in enumerate(patch.operations):
        try:
            target_file = _normalize_project_relative_path(operation.target_file)
        except ValueError as exc:
            reasons.append(f"Operation {index} has unsafe target_file: {exc}")
            continue
        if target_file not in ALLOWED_CANON_TARGETS:
            reasons.append(f"Operation {index} targets disallowed file: {target_file}.")
        if not operation.target_id.strip():
            reasons.append(f"Operation {index} target_id cannot be empty.")
    return reasons


def _validate_versions(project_path: Path, patch: CandidateStatePatch) -> list[str]:
    reasons: list[str] = []
    for index, operation in enumerate(patch.operations):
        try:
            target_file = _normalize_project_relative_path(operation.target_file)
        except ValueError:
            continue
        if target_file not in ALLOWED_CANON_TARGETS:
            continue

        try:
            state = _read_versioned_state(project_path / target_file)
        except (FileNotFoundError, ValueError) as exc:
            reasons.append(f"Operation {index} cannot read target state: {exc}")
            continue

        if state.version != operation.expected_version:
            reasons.append(
                "Operation "
                f"{index} expected {target_file} version {operation.expected_version}, "
                f"found {state.version}."
            )
    return reasons


def _validate_evidence(project_path: Path, patch: CandidateStatePatch) -> list[str]:
    reasons: list[str] = []
    based_on_files_by_key: dict[str, str] = {}
    for key, path in patch.based_on.items():
        expected_name = ALLOWED_PATCH_SOURCE_FILES.get(key)
        if expected_name is None:
            reasons.append(f"Patch based_on has unsupported source key: {key}.")
        try:
            normalized_path = _normalize_project_relative_path(path)
        except ValueError as exc:
            reasons.append(f"Patch based_on {key} has unsafe path: {exc}")
            continue
        if expected_name is not None and Path(normalized_path).name != expected_name:
            reasons.append(
                f"Patch based_on {key} must reference {expected_name}, got {normalized_path}."
            )
        based_on_files_by_key[key] = normalized_path
    based_on_files = set(based_on_files_by_key.values())

    final_path = based_on_files_by_key.get("chapter_final")
    observations_path = based_on_files_by_key.get("observations")
    final_text = _read_text_if_present(project_path / final_path) if final_path else None

    if not final_path:
        reasons.append("Patch based_on must include chapter_final.")
    elif final_text is None:
        reasons.append(f"chapter_final evidence file does not exist: {final_path}.")

    reasons.extend(
        _validate_observations_source(
            project_path,
            final_path=final_path,
            observations_path=observations_path,
        )
    )

    for index, operation in enumerate(patch.operations):
        if not operation.evidence:
            reasons.append(f"Operation {index} must include evidence.")
            continue

        has_final_evidence = False
        for evidence_index, evidence in enumerate(operation.evidence):
            try:
                evidence_file = _normalize_project_relative_path(evidence.file)
            except ValueError as exc:
                reasons.append(
                    f"Operation {index} evidence {evidence_index} has unsafe file: {exc}"
                )
                continue

            if evidence_file not in based_on_files:
                reasons.append(
                    f"Operation {index} evidence {evidence_index} references "
                    f"non-source file: {evidence_file}."
                )
            if final_path and evidence_file != final_path:
                reasons.append(
                    f"Operation {index} evidence {evidence_index} must cite chapter_final; "
                    f"{evidence_file} is not committed final evidence."
                )
            if not evidence.quote.strip():
                reasons.append(f"Operation {index} evidence {evidence_index} quote is empty.")
                continue
            if evidence_file == final_path:
                has_final_evidence = True
                if final_text is not None and evidence.quote not in final_text:
                    reasons.append(
                        f"Operation {index} evidence {evidence_index} quote "
                        "is not present in chapter_final."
                    )

        if not has_final_evidence:
            reasons.append(f"Operation {index} must cite chapter_final evidence.")
    return reasons


def _validate_observations_source(
    project_path: Path,
    *,
    final_path: str | None,
    observations_path: str | None,
) -> list[str]:
    reasons: list[str] = []
    if not observations_path:
        return ["Patch based_on must include observations."]

    observations_json_invalid = False
    try:
        observations_payload = read_json(project_path / observations_path, default=None)
    except ValueError as exc:
        reasons.append(f"observations source is not valid JSON: {exc}")
        observations_json_invalid = True
        observations_payload = None

    if observations_payload is None and not observations_json_invalid:
        reasons.append(f"observations source file does not exist: {observations_path}.")
    elif observations_payload is not None and not isinstance(observations_payload, dict):
        reasons.append(f"observations source must be a JSON object: {observations_path}.")
    elif isinstance(observations_payload, dict):
        status = observations_payload.get("status")
        if status != OBSERVATIONS_REQUIRED_STATUS:
            reasons.append(
                "observations source must have status "
                f"{OBSERVATIONS_REQUIRED_STATUS}, got {status!r}."
            )
        reasons.extend(
            _validate_observations_draft_source(
                project_path,
                observations_payload,
                observations_path=observations_path,
                final_path=final_path,
            )
        )

    if final_path and Path(final_path).parent != Path(observations_path).parent:
        reasons.append(
            "Patch based_on chapter_final and observations must belong to the same chapter."
        )
    return reasons


def _validate_observations_draft_source(
    project_path: Path,
    observations_payload: dict[str, object],
    *,
    observations_path: str,
    final_path: str | None,
) -> list[str]:
    reasons: list[str] = []
    based_on = observations_payload.get("based_on")
    if not isinstance(based_on, str) or not based_on.strip():
        return ["observations source must record based_on draft path."]

    try:
        draft_path = _normalize_project_relative_path(based_on)
    except ValueError as exc:
        return [f"observations based_on has unsafe path: {exc}"]

    if Path(draft_path).name != "draft.md":
        reasons.append(f"observations based_on must reference draft.md, got {draft_path}.")
    if Path(draft_path).parent != Path(observations_path).parent:
        reasons.append("observations based_on draft must belong to the same chapter.")
    if final_path and Path(draft_path).parent != Path(final_path).parent:
        reasons.append("observations based_on draft must belong to chapter_final's chapter.")
    if not (project_path / draft_path).exists():
        reasons.append(f"observations based_on draft file does not exist: {draft_path}.")
    return reasons


def _validate_conflicts(project_path: Path, patch: CandidateStatePatch) -> list[str]:
    reasons: list[str] = []
    states: dict[str, VersionedState] = {}

    for index, operation in enumerate(patch.operations):
        try:
            target_file = _normalize_project_relative_path(operation.target_file)
        except ValueError:
            continue
        if target_file not in ALLOWED_CANON_TARGETS:
            continue

        state = states.get(target_file)
        if state is None:
            try:
                state = _read_versioned_state(project_path / target_file)
            except (FileNotFoundError, ValueError):
                continue
            states[target_file] = state

        current_value = state.items.get(operation.target_id)
        if operation.op == "delete" and operation.target_id not in state.items:
            reasons.append(
                f"Operation {index} cannot delete missing item {operation.target_id}."
            )
        if operation.op == "append" and current_value is not None and not isinstance(
            current_value,
            list,
        ):
            reasons.append(
                f"Operation {index} cannot append to non-list item {operation.target_id}."
            )
    return reasons


def _load_target_states(
    project_path: Path,
    operations: list[CandidatePatchOperation],
) -> dict[str, VersionedState]:
    states: dict[str, VersionedState] = {}
    for operation in operations:
        target_file = _normalize_project_relative_path(operation.target_file)
        if target_file not in states:
            states[target_file] = _read_versioned_state(project_path / target_file)
    return states


def _apply_operation(state: VersionedState, operation: CandidatePatchOperation) -> None:
    if operation.op == "upsert":
        state.items[operation.target_id] = operation.value
        return
    if operation.op == "delete":
        del state.items[operation.target_id]
        return

    current_value = state.items.setdefault(operation.target_id, [])
    if not isinstance(current_value, list):
        raise PatchValidationError(
            PatchValidationResult(
                schema="passed",
                versions="passed",
                evidence="passed",
                conflicts="failed",
                reasons=[f"Cannot append to non-list item {operation.target_id}."],
            )
        )
    current_value.append(operation.value)


def _read_versioned_state(path: Path) -> VersionedState:
    data = read_json(path)
    if data is None:
        raise FileNotFoundError(path)
    try:
        return VersionedState.model_validate(data)
    except ValueError as exc:
        raise ValueError(f"Invalid versioned state at {path}") from exc


def _read_text_if_present(path: Path) -> str | None:
    if not path.exists():
        return None
    return read_text_file(path)


def _normalize_project_relative_path(path: str) -> str:
    return ensure_relative_artifact_path(path).as_posix()
