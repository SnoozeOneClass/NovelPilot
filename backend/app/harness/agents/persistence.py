import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from app.core.paths import ensure_relative_artifact_path
from app.harness.agents.models import (
    AgentIdentity,
    AgentState,
    CandidateKind,
    EvaluationHistoryEntry,
    EvaluationInput,
    EvaluationRecord,
    RepairChain,
    RepairChainEntry,
    RepairContract,
    ToolReplayRecord,
)
from app.harness.agents.rubrics import changed_components
from app.storage.file_lock import exclusive_file_lock
from app.storage.json_files import read_json, write_json
from app.storage.transactions import commit_file_transaction


ActivationLog = Literal["transcript", "tool-calls", "events"]


def agent_scope_relative(identity: AgentIdentity) -> Path:
    if identity.role == "book":
        relative = Path("book") / "agent"
    elif identity.role == "story_arc":
        relative = Path("arcs") / _safe_scope_id(identity) / "agent"
    else:
        relative = Path("chapters") / _safe_scope_id(identity) / "agent"
    return ensure_relative_artifact_path(relative.as_posix())


def activation_relative(identity: AgentIdentity, activation_id: str) -> Path:
    safe_activation = ensure_relative_artifact_path((Path("a") / activation_id).as_posix())
    return agent_scope_relative(identity) / safe_activation


def repair_chain_relative(identity: AgentIdentity) -> Path:
    return agent_scope_relative(identity) / "repair-chain.json"


def read_repair_chain(
    project_path: Path,
    identity: AgentIdentity,
    *,
    candidate_run_id: str,
    candidate_kind: CandidateKind,
    semantic_revision_limit: int,
) -> RepairChain:
    payload = read_json(project_path / repair_chain_relative(identity), default=None)
    if payload is not None:
        chain = RepairChain.model_validate(payload)
        if chain.identity == identity and chain.candidate_run_id == candidate_run_id:
            if chain.candidate_kind != candidate_kind:
                raise ValueError("Repair chain candidate kind does not match the Agent run.")
            return chain
    return RepairChain(
        identity=identity,
        candidate_run_id=candidate_run_id,
        candidate_kind=candidate_kind,
        semantic_revision_limit=semantic_revision_limit,
    )


def save_repair_chain(project_path: Path, chain: RepairChain) -> None:
    root = project_path / agent_scope_relative(chain.identity)
    with exclusive_file_lock(root / ".repair-chain.lock"):
        write_json(
            project_path / repair_chain_relative(chain.identity),
            chain.model_dump(mode="json"),
        )


def append_repair_chain_evaluation(
    project_path: Path,
    chain: RepairChain,
    *,
    activation_id: str,
    evaluation_path: str,
    evaluation_input: EvaluationInput,
    evaluation: EvaluationRecord,
) -> RepairChain:
    if evaluation.candidate_run_id != chain.candidate_run_id:
        raise ValueError("Evaluation does not belong to the repair chain candidate run.")
    if evaluation.candidate_revision != evaluation_input.candidate_revision:
        raise ValueError("Evaluation revision does not match its immutable input.")
    existing = next(
        (item for item in chain.entries if item.evaluation_id == evaluation.evaluation_id),
        None,
    )
    if existing is not None:
        return chain
    if len(chain.entries) + 1 != evaluation_input.candidate_revision:
        raise ValueError("Repair chain logical candidate revision is not sequential.")
    prior_fingerprints = (
        chain.entries[-1].component_fingerprints if chain.entries else None
    )
    changed = (
        changed_components(
            prior_fingerprints,
            evaluation_input.component_fingerprints,
        )
        if prior_fingerprints is not None
        else []
    )
    if evaluation_input.expected_repair is not None:
        unexpected = set(changed) - set(
            evaluation_input.expected_repair.allowed_components
        )
        if unexpected:
            raise ValueError(
                "Repaired candidate changed components outside the repair contract: "
                + ", ".join(sorted(unexpected))
            )
    entry = RepairChainEntry(
        activation_id=activation_id,
        candidate_artifact_id=evaluation.candidate_artifact_id,
        candidate_revision=evaluation.candidate_revision,
        component_fingerprints=evaluation_input.component_fingerprints,
        evaluation_id=evaluation.evaluation_id,
        evaluation_path=evaluation_path,
        changed_components=changed,
        open_issue_ids=[
            issue.issue_id
            for issue in evaluation.result.issues
            if issue.issue_id is not None
        ],
        resolved_issue_ids=evaluation.result.resolved_issue_ids,
        new_issue_ids=evaluation.result.new_issue_ids,
    )
    history_entry = EvaluationHistoryEntry(
        evaluation_id=evaluation.evaluation_id,
        candidate_revision=evaluation.candidate_revision,
        candidate_artifact_id=evaluation.candidate_artifact_id,
        component_fingerprints=evaluation_input.component_fingerprints,
        result=evaluation.result,
    )
    revised = chain.model_copy(
        update={
            "entries": [*chain.entries, entry],
            "review_history": [*chain.review_history, history_entry],
            "pending_repair": None,
        }
    )
    save_repair_chain(project_path, revised)
    return revised


def persist_pending_repair(
    project_path: Path,
    chain: RepairChain,
    contract: RepairContract,
) -> RepairChain:
    if contract.source_candidate_revision != len(chain.entries):
        raise ValueError("Repair contract source revision is not the chain head.")
    if chain.pending_repair is not None:
        if chain.pending_repair == contract:
            return chain
        raise ValueError("Repair chain already has a different pending repair.")
    revised = chain.model_copy(
        update={
            "used_semantic_revisions": chain.used_semantic_revisions + 1,
            "pending_repair": contract,
        }
    )
    save_repair_chain(project_path, revised)
    return revised


def read_agent_state(project_path: Path, identity: AgentIdentity) -> AgentState:
    path = project_path / agent_scope_relative(identity) / "state.json"
    payload = read_json(path, default=None)
    if payload is None:
        return AgentState(identity=identity)
    state = AgentState.model_validate(payload)
    if state.identity != identity:
        raise ValueError("Persisted Agent identity does not match its storage scope.")
    if state.lifecycle == "running":
        state.lifecycle = "failed"
        state.summary = "Previous activation was interrupted before a durable terminal result."
        save_agent_state(project_path, state)
    return state


def save_agent_state(project_path: Path, state: AgentState) -> None:
    state.updated_at = datetime.now(UTC)
    root = project_path / agent_scope_relative(state.identity)
    with exclusive_file_lock(root / ".state.lock"):
        write_json(root / "state.json", state.model_dump(mode="json"))


def append_activation_log(
    project_path: Path,
    identity: AgentIdentity,
    activation_id: str,
    log: ActivationLog,
    payload: dict[str, Any],
) -> str:
    relative = activation_relative(identity, activation_id) / f"{log}.jsonl"
    path = project_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_file_lock(path.with_suffix(path.suffix + ".lock")):
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            handle.flush()
    return relative.as_posix()


def write_activation_document(
    project_path: Path,
    identity: AgentIdentity,
    activation_id: str,
    name: str,
    payload: object,
) -> str:
    if not name or "/" in name or "\\" in name or name in {".", ".."}:
        raise ValueError("Activation document name must be a safe filename.")
    relative = activation_relative(identity, activation_id) / name
    write_json(project_path / relative, payload)
    return relative.as_posix()


def clone_activation_candidate_workspace(
    project_path: Path,
    identity: AgentIdentity,
    *,
    source_activation_id: str,
    target_activation_id: str,
) -> list[str]:
    """Copy only uncommitted candidate files into a fresh bounded activation."""
    source_root = project_path / activation_relative(identity, source_activation_id) / "c"
    if not source_root.is_dir():
        return []
    target_root = activation_relative(identity, target_activation_id) / "c"
    files: dict[str, str | bytes] = {}
    for source in sorted(source_root.rglob("*")):
        if (
            not source.is_file()
            or source.name.endswith(".tmp")
            or source.name == "repair-workspace.json"
        ):
            continue
        relative = target_root / source.relative_to(source_root)
        files[relative.as_posix()] = source.read_bytes()
    if not files:
        return []
    commit_file_transaction(
        project_path,
        kind=f"agent-candidate-retry-seed-{target_activation_id}",
        files=files,
    )
    return sorted(files)


def idempotency_record_relative(
    identity: AgentIdentity,
    activation_id: str,
    tool_call_id: str,
) -> Path:
    filename = sha256(tool_call_id.encode("utf-8")).hexdigest()[:12] + ".json"
    return activation_relative(identity, activation_id) / "i" / filename


def read_tool_replay(
    project_path: Path,
    identity: AgentIdentity,
    activation_id: str,
    tool_call_id: str,
) -> ToolReplayRecord | None:
    payload = read_json(
        project_path
        / idempotency_record_relative(identity, activation_id, tool_call_id),
        default=None,
    )
    if payload is None:
        return None
    return ToolReplayRecord.model_validate(payload)


def argument_digest(arguments: dict[str, Any]) -> str:
    canonical = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def json_document(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def _safe_scope_id(identity: AgentIdentity) -> str:
    if identity.scope_id is None:
        raise ValueError(f"{identity.role} Agent identity is missing its scope ID.")
    ensure_relative_artifact_path(identity.scope_id)
    if len(Path(identity.scope_id).parts) != 1:
        raise ValueError("Agent scope ID cannot contain path separators.")
    return identity.scope_id
