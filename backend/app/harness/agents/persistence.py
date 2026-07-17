import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

from app.core.paths import ensure_relative_artifact_path
from app.harness.agents.models import AgentIdentity, AgentState, ToolReplayRecord
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
        if not source.is_file() or source.name.endswith(".tmp"):
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
