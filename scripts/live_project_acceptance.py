from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Sequence


ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.api import arcs as arcs_api  # noqa: E402
from app.api import profiles as profiles_api  # noqa: E402
from app.api import projects as projects_api  # noqa: E402
from app.api import readiness as readiness_api  # noqa: E402
from app.api import runs as runs_api  # noqa: E402
from app.api import setup as setup_api  # noqa: E402
from app.harness.run_host import get_run_host  # noqa: E402
from app.llm.redaction import (  # noqa: E402
    profile_secret_values,
    redact_sensitive_values,
)
from app.schemas.arcs import CurrentArcApprovalRequest  # noqa: E402
from app.schemas.projects import CreateProjectRequest  # noqa: E402
from app.schemas.setup import (  # noqa: E402
    SetupApprovalRequest,
    SetupStateDocument,
    SetupSuggestion,
    SetupTurnRequest,
)
from app.storage import arcs as arc_storage  # noqa: E402
from app.storage import profiles as profile_storage  # noqa: E402
from app.storage import projects as project_storage  # noqa: E402
from app.storage.events import read_events  # noqa: E402
from app.storage.json_files import read_json, write_json  # noqa: E402
from app.storage.run_state import read_run_control_state  # noqa: E402
from app.storage.secret_audit import audit_path_for_profile_secrets  # noqa: E402
from scripts.live_provider_smoke import (  # noqa: E402
    LiveProviderSmokeError,
    _call_user_action,
    _load_enabled_profile,
    _restore_runtime_state,
    _select_profile_id,
    _test_profile,
)


DEFAULT_CASE_PATH = (
    ROOT_DIR / "scripts" / "live_acceptance_cases" / "phase16_two_chapter.json"
)
REPORT_RELATIVE = Path("exports") / "live_project_acceptance_report.json"


class LiveProjectAcceptanceError(RuntimeError):
    def __init__(self, message: str, *, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class LiveProjectAcceptanceOptions:
    profile_id: str | None
    case_path: Path = DEFAULT_CASE_PATH
    skip_profile_test: bool = False
    keep_active: bool = False
    timeout_seconds: float = 1_800
    poll_interval_seconds: float = 0.5


@dataclass
class RecommendedOnlyActor:
    policy_version: str
    decisions: list[dict[str, object]] = field(default_factory=list)

    def select(self, state: SetupStateDocument) -> SetupSuggestion:
        recommended = [item for item in state.suggestions if item.recommended]
        if len(recommended) != 1:
            raise LiveProjectAcceptanceError(
                "The public Book decision did not expose exactly one model "
                f"recommendation: question={state.question!r}, "
                f"recommended_count={len(recommended)}."
            )
        selected = recommended[0]
        self.decisions.append(
            {
                "gate": "book_question",
                "question": state.question,
                "question_sha256": _text_sha256(state.question),
                "suggestion_id": selected.id,
                "label": selected.label,
                "message": selected.message,
                "suggestion_text_sha256": _text_sha256(selected.message),
                "recommended": selected.recommended,
                "selection": "unique_model_recommendation",
                "api_command": "POST /api/setup/turn",
            }
        )
        return selected

    def record_book_approval(self, state: SetupStateDocument) -> None:
        candidate = state.candidate
        if candidate is None or state.selected_title is None:
            raise LiveProjectAcceptanceError("Book approval evidence is incomplete.")
        self.decisions.append(
            {
                "gate": "book_candidate_approval",
                "candidate_revision": candidate.revision,
                "title": state.selected_title,
                "selection": "exact_reviewed_candidate",
                "api_command": "POST /api/setup/approve",
            }
        )

    def record_arc_approval(
        self,
        *,
        arc_id: str,
        recommended_target_chapter_count: int,
    ) -> None:
        self.decisions.append(
            {
                "gate": "story_arc_approval",
                "arc_id": arc_id,
                "target_chapter_count": recommended_target_chapter_count,
                "selection": "exact_model_recommendation",
                "api_command": "POST /api/arcs/current/approve",
            }
        )

    def record_result(self, *, gate: str, project_path: Path) -> None:
        for decision in reversed(self.decisions):
            if decision.get("gate") != gate or "resulting_event_seq" in decision:
                continue
            events = read_events(project_path)
            decision["resulting_event_seq"] = events[-1].seq if events else None
            return
        raise LiveProjectAcceptanceError(
            f"Cannot attach public API result evidence to actor gate {gate!r}."
        )


def run_live_project_acceptance(
    options: LiveProjectAcceptanceOptions,
) -> dict[str, object]:
    case, prompt = _load_case(options.case_path)
    previous_project_path = project_storage.get_active_project_path()
    previous_profile_id = profile_storage.load_profiles().active_profile_id
    try:
        profile_id = _select_profile_id(options.profile_id)
        profile = _load_enabled_profile(profile_id)
    except LiveProviderSmokeError as exc:
        raise LiveProjectAcceptanceError(
            str(exc),
            exit_code=exc.exit_code,
        ) from exc
    redaction_values = profile_secret_values(profile)
    actor = RecommendedOnlyActor(policy_version=str(case["actor_policy_version"]))
    project_path: Path | None = None
    start_receipt: dict[str, object] | None = None
    host = get_run_host()
    host_started_here = not host.started

    try:
        _assert_no_competing_runnable_projects()
        project = projects_api.create_project(
            CreateProjectRequest(operation_mode="participatory")
        )
        project_path = Path(project.path)
        profiles_api.select_profile(profile.id)
        profile_test = _test_profile(
            profile.id,
            options.skip_profile_test,
            redaction_values,
        )
        setup_state = _complete_book_setup(
            actor,
            prompt,
            project_path,
            redaction_values,
        )
        if not setup_state.approved:
            raise LiveProjectAcceptanceError("Book setup did not reach approved state.")

        _assert_no_competing_runnable_projects(exclude=project_path)
        if host_started_here:
            host.start()
        start_receipt = _call_user_action(
            "start the asynchronous Harness run",
            runs_api.start_run,
            redaction_values,
        )
        if start_receipt["dispatch_status"] != "accepted":
            raise LiveProjectAcceptanceError(
                "The live run was not durably accepted by RunHost: "
                + _redact_json(start_receipt, redaction_values)
            )

        terminal = _drive_normal_user_gates(
            project_path,
            actor,
            case,
            redaction_values,
            timeout_seconds=options.timeout_seconds,
            poll_interval_seconds=options.poll_interval_seconds,
        )
        chapter_contract_evidence = _assert_two_chapter_contract(project_path, case)
        secret_audit = audit_path_for_profile_secrets(project_path, [profile])
        if secret_audit.findings:
            raise LiveProjectAcceptanceError(
                "Provider secrets or endpoint configuration leaked into project output: "
                + ", ".join(
                    f"{item.path}:{item.kind}" for item in secret_audit.findings
                )
            )

        report = _build_report(
            status="passed",
            project_path=project_path,
            case=case,
            actor=actor,
            profile_id=profile.id,
            model_snapshot=profile_test.model_snapshot,
            provider_snapshot=profile_test.provider_snapshot,
            start_receipt=start_receipt,
            terminal=terminal,
            chapter_contract_evidence=chapter_contract_evidence,
            failure=None,
        )
        write_json(project_path / REPORT_RELATIVE, report)
        post_report_audit = audit_path_for_profile_secrets(project_path, [profile])
        if post_report_audit.findings:
            raise LiveProjectAcceptanceError(
                "The redacted acceptance report failed the output secret audit."
            )
        return report
    except LiveProjectAcceptanceError as exc:
        if project_path is not None:
            report = _build_report(
                status="failed",
                project_path=project_path,
                case=case,
                actor=actor,
                profile_id=profile.id,
                model_snapshot=profile.model,
                provider_snapshot=profile.protocol,
                start_receipt=start_receipt,
                terminal=None,
                chapter_contract_evidence=None,
                failure=redact_sensitive_values(str(exc), redaction_values),
            )
            write_json(project_path / REPORT_RELATIVE, report)
        raise
    except Exception as exc:
        error = LiveProjectAcceptanceError(
            "Unexpected live full-project acceptance failure: "
            + redact_sensitive_values(str(exc), redaction_values)
        )
        if project_path is not None:
            report = _build_report(
                status="failed",
                project_path=project_path,
                case=case,
                actor=actor,
                profile_id=profile.id,
                model_snapshot=profile.model,
                provider_snapshot=profile.protocol,
                start_receipt=start_receipt,
                terminal=None,
                chapter_contract_evidence=None,
                failure=str(error),
            )
            write_json(project_path / REPORT_RELATIVE, report)
        raise error from exc
    finally:
        if host_started_here and host.started:
            host.stop(timeout=30)
        if not options.keep_active:
            _restore_runtime_state(previous_project_path, previous_profile_id)


def _complete_book_setup(
    actor: RecommendedOnlyActor,
    prompt: str,
    project_path: Path,
    redaction_values: Sequence[str],
) -> SetupStateDocument:
    state = _call_user_action(
        "read Book setup state",
        setup_api.get_setup_state,
        redaction_values,
    )
    initial_brief_sent = False
    for _step in range(24):
        candidate = state.candidate
        if candidate is not None and candidate.approval_allowed:
            if state.selected_title is None:
                raise LiveProjectAcceptanceError(
                    "The reviewed Book candidate has no model-recommended formal title."
                )
            actor.record_book_approval(state)
            approved = _call_user_action(
                "approve the exact reviewed Book candidate",
                lambda: setup_api.approve_setup(
                    SetupApprovalRequest(
                        candidate_revision=candidate.revision,
                        title=state.selected_title,
                    )
                ),
                redaction_values,
            )
            actor.record_result(gate="book_candidate_approval", project_path=project_path)
            return approved

        if state.question is not None:
            selection = actor.select(state)
            state = _call_user_action(
                "answer the Book question with its unique recommendation",
                lambda: setup_api.continue_setup_discussion(
                    SetupTurnRequest(message=selection.message)
                ),
                redaction_values,
            )
            actor.record_result(gate="book_question", project_path=project_path)
            continue

        if not initial_brief_sent:
            state = _call_user_action(
                "send the live acceptance creator brief",
                lambda: setup_api.continue_setup_discussion(
                    SetupTurnRequest(message=prompt)
                ),
                redaction_values,
            )
            initial_brief_sent = True
            continue

        if state.readiness.status == "ready" or candidate is not None:
            state = _call_user_action(
                "prepare the bounded Book Direction review",
                setup_api.prepare_setup_review,
                redaction_values,
            )
            continue

        raise LiveProjectAcceptanceError(
            "Book discussion requested more input without exposing one question and "
            "one unique model recommendation."
        )
    raise LiveProjectAcceptanceError(
        "Book Direction did not converge within 24 public user actions."
    )


def _assert_no_competing_runnable_projects(*, exclude: Path | None = None) -> None:
    competing: list[str] = []
    excluded = exclude.resolve() if exclude is not None else None
    for project in project_storage.list_projects():
        path = Path(project.path)
        if excluded is not None and path.resolve() == excluded:
            continue
        state = read_run_control_state(path)
        if state.desired_state != "running":
            continue
        if project.metadata.run_status not in {
            "idle",
            "running",
            "waiting_for_provider",
        }:
            continue
        competing.append(f"{project.name}:{project.metadata.run_status}")
    if competing:
        raise LiveProjectAcceptanceError(
            "Live acceptance requires an idle RunHost queue; resolve other runnable "
            "projects first: " + ", ".join(sorted(competing))
        )


def _drive_normal_user_gates(
    project_path: Path,
    actor: RecommendedOnlyActor,
    case: dict[str, object],
    redaction_values: Sequence[str],
    *,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    expected_target = int(_nested(case, "first_arc", "expected_target_chapter_count"))
    first_arc_approved = False

    while time.monotonic() < deadline:
        metadata = project_storage.read_project_metadata(project_path)
        readiness = readiness_api.get_readiness()
        current_arc = arcs_api.get_current_arc()

        if _is_terminal(project_path, readiness.next_action.id):
            return {
                "project_status": metadata.run_status,
                "readiness_action": readiness.next_action.id,
                "active_arc_id": metadata.active_arc_id,
                "event_count": len(read_events(project_path)),
            }
        if metadata.run_status == "failed":
            raise LiveProjectAcceptanceError(
                "The normal live flow reached a natural Harness failure. "
                + _failure_diagnostic(project_path, redaction_values)
            )
        if metadata.run_status == "paused":
            raise LiveProjectAcceptanceError(
                "The normal live flow paused unexpectedly. "
                + _failure_diagnostic(project_path, redaction_values)
            )

        if readiness.next_action.id == "approve_story_arc":
            if current_arc is None:
                raise LiveProjectAcceptanceError(
                    "Readiness requested Story Arc approval without a current Arc."
                )
            if current_arc.arc_id != "arc-001" or first_arc_approved:
                raise LiveProjectAcceptanceError(
                    "An unexpected Story Arc approval gate appeared before the terminal "
                    f"checkpoint: arc={current_arc.arc_id}."
                )
            recommended = current_arc.recommended_target_chapter_count
            if recommended != expected_target:
                raise LiveProjectAcceptanceError(
                    "The model recommendation did not honor the two-Chapter case: "
                    f"recommended={recommended}, expected={expected_target}."
                )
            actor.record_arc_approval(
                arc_id=current_arc.arc_id,
                recommended_target_chapter_count=recommended,
            )
            _call_user_action(
                "approve the exact recommended Story Arc target",
                lambda: arcs_api.approve_current_arc(
                    CurrentArcApprovalRequest(target_chapter_count=recommended)
                ),
                redaction_values,
            )
            actor.record_result(gate="story_arc_approval", project_path=project_path)
            first_arc_approved = True
            continue

        if readiness.next_action.requires_user:
            raise LiveProjectAcceptanceError(
                "The normal live driver encountered an unsupported human action: "
                f"{readiness.next_action.id}. "
                + _failure_diagnostic(project_path, redaction_values)
            )
        time.sleep(max(0.05, min(poll_interval_seconds, 2.0)))

    raise LiveProjectAcceptanceError(
        f"Timed out after {timeout_seconds:.1f}s while observing the normal live flow. "
        + _failure_diagnostic(project_path, redaction_values)
    )


def _is_terminal(project_path: Path, readiness_action: str) -> bool:
    first_arc = arc_storage.read_arc_state(project_path, "arc-001")
    current_arc = arcs_api.get_current_arc()
    finals = [
        project_path / "chapters" / chapter_id / "final.md"
        for chapter_id in ("chapter-001", "chapter-002")
    ]
    return bool(
        first_arc is not None
        and first_arc.status == "completed"
        and first_arc.completed_chapter_ids == ["chapter-001", "chapter-002"]
        and current_arc is not None
        and current_arc.arc_id == "arc-002"
        and current_arc.human_review == "awaiting_review"
        and readiness_action == "approve_story_arc"
        and all(path.is_file() for path in finals)
    )


def _assert_two_chapter_contract(
    project_path: Path,
    case: dict[str, object],
) -> dict[str, object]:
    required = [
        "book/direction.md",
        "book/constraints.json",
        "arcs/arc-001/plan.md",
        "chapters/chapter-001/final.md",
        "chapters/chapter-001/verification.json",
        "chapters/chapter-001/committed_state_patch.json",
        "chapters/chapter-002/context_snapshot.json",
        "chapters/chapter-002/final.md",
        "chapters/chapter-002/verification.json",
        "chapters/chapter-002/committed_state_patch.json",
        "arcs/arc-002/plan.md",
    ]
    missing = [path for path in required if not (project_path / path).is_file()]
    if missing:
        raise LiveProjectAcceptanceError(
            f"The two-Chapter flow is missing required artifacts: {missing}."
        )

    chapter_1 = (project_path / "chapters/chapter-001/final.md").read_text(
        encoding="utf-8"
    )
    chapter_2 = (project_path / "chapters/chapter-002/final.md").read_text(
        encoding="utf-8"
    )
    required_chapter_1 = ("林澈", "许青", "23:17", "0417")
    required_chapter_2 = ("林澈", "许青", "0417")
    absent = [value for value in required_chapter_1 if value not in chapter_1]
    absent.extend(value for value in required_chapter_2 if value not in chapter_2)
    if absent:
        raise LiveProjectAcceptanceError(
            "Cross-Chapter stable facts are absent from committed prose: "
            f"{sorted(set(absent))}."
        )

    context = read_json(project_path / "chapters/chapter-002/context_snapshot.json")
    serialized_context = json.dumps(context, ensure_ascii=False)
    if "chapter-001" not in serialized_context:
        raise LiveProjectAcceptanceError(
            "Chapter 2 context does not cite committed Chapter 1 evidence."
        )
    if int(_nested(case, "first_arc", "expected_target_chapter_count")) != 2:
        raise LiveProjectAcceptanceError("The live case no longer specifies two Chapters.")

    events = read_events(project_path)
    chapter_events: dict[str, dict[str, int]] = {}
    budget_evidence: dict[str, dict[str, object]] = {}
    for chapter_id in ("chapter-001", "chapter-002"):
        expected_events = {
            "draft_stream_started": next(
                (
                    event.seq
                    for event in events
                    if event.kind == "chapter_draft_stream_started"
                    and event.payload.get("chapter_id") == chapter_id
                ),
                None,
            ),
            "evaluation_completed": next(
                (
                    event.seq
                    for event in events
                    if event.kind == "verification_completed"
                    and event.artifact_path
                    == f"chapters/{chapter_id}/verification.json"
                ),
                None,
            ),
            "prose_promoted": next(
                (
                    event.seq
                    for event in events
                    if event.kind == "artifact_written"
                    and event.artifact_path == f"chapters/{chapter_id}/final.md"
                ),
                None,
            ),
            "canon_committed": next(
                (
                    event.seq
                    for event in events
                    if event.kind == "state_patch_committed"
                    and event.artifact_path
                    == f"chapters/{chapter_id}/committed_state_patch.json"
                ),
                None,
            ),
            "chapter_checkpoint": next(
                (
                    event.seq
                    for event in events
                    if event.kind == "safe_checkpoint_reached"
                    and event.atomic_action == "chapter_complete"
                    and event.payload.get("chapter_id") == chapter_id
                ),
                None,
            ),
        }
        missing_events = [
            name for name, sequence in expected_events.items() if sequence is None
        ]
        if missing_events:
            raise LiveProjectAcceptanceError(
                f"{chapter_id} is missing normal-path event evidence: {missing_events}."
            )
        chapter_events[chapter_id] = {
            name: int(sequence)
            for name, sequence in expected_events.items()
            if sequence is not None
        }
        budget_evidence[chapter_id] = _initial_chapter_budget_evidence(
            project_path,
            chapter_id,
        )

    return {
        "normal_path_event_sequences": chapter_events,
        "initial_action_local_budgets": budget_evidence,
        "chapter_2_context_cites_chapter_1": True,
    }


def _initial_chapter_budget_evidence(
    project_path: Path,
    chapter_id: str,
) -> dict[str, object]:
    requests: list[tuple[str, Path, dict[str, object]]] = []
    for path in (project_path / "chapters" / chapter_id / "agent" / "a").glob(
        "*/request.json"
    ):
        payload = read_json(path, default={})
        if isinstance(payload, dict):
            requests.append((str(payload.get("created_at", "")), path, payload))
    if not requests:
        raise LiveProjectAcceptanceError(
            f"{chapter_id} has no Chapter Agent request snapshot."
        )
    _, request_path, request = min(requests, key=lambda item: item[0])
    budgets = request.get("budgets")
    if not isinstance(budgets, dict):
        raise LiveProjectAcceptanceError(
            f"{chapter_id} initial request has no typed budget snapshot."
        )
    expected_zero = (
        "used_turns",
        "used_tool_schema_repairs",
        "used_semantic_revisions",
        "used_transport_retries",
    )
    leaked = {
        key: budgets.get(key)
        for key in expected_zero
        if budgets.get(key) != 0
    }
    if request.get("retry_budget_scope_version") != "action-local-v1" or leaked:
        raise LiveProjectAcceptanceError(
            f"{chapter_id} did not begin with an independent action-local budget: "
            f"scope={request.get('retry_budget_scope_version')!r}, leaked={leaked}."
        )
    return {
        "request_path": request_path.relative_to(project_path).as_posix(),
        "activation_id": request.get("activation_id"),
        "candidate_run_id": request.get("candidate_run_id"),
        "retry_budget_scope_version": request.get("retry_budget_scope_version"),
        "initial_usage": {key: budgets[key] for key in expected_zero},
    }


def _build_report(
    *,
    status: str,
    project_path: Path,
    case: dict[str, object],
    actor: RecommendedOnlyActor,
    profile_id: str,
    model_snapshot: str,
    provider_snapshot: str,
    start_receipt: dict[str, object] | None,
    terminal: dict[str, object] | None,
    chapter_contract_evidence: dict[str, object] | None,
    failure: str | None,
) -> dict[str, object]:
    events = read_events(project_path)
    checkpoints_root = project_path / "book" / "harness" / "checkpoints"
    checkpoints = [
        read_json(path)
        for path in sorted(checkpoints_root.glob("*.json"))
    ] if checkpoints_root.is_dir() else []
    evaluations = []
    for path in sorted(project_path.rglob("evaluation.json")):
        payload = read_json(path, default={})
        if not isinstance(payload, dict):
            continue
        evaluations.append(
            {
                "path": path.relative_to(project_path).as_posix(),
                "evaluation_id": payload.get("evaluation_id"),
                "candidate_run_id": payload.get("candidate_run_id"),
                "candidate_revision": payload.get("candidate_revision"),
                "input_fingerprint": payload.get("input_fingerprint"),
                "outcome": (
                    payload.get("result", {}).get("outcome")
                    if isinstance(payload.get("result"), dict)
                    else None
                ),
            }
        )
    run_state = read_run_control_state(project_path)
    readiness = readiness_api.get_readiness()
    first_arc = arc_storage.read_arc_state(project_path, "arc-001")
    current_arc = arcs_api.get_current_arc()
    artifacts = [
        path.relative_to(project_path).as_posix()
        for path in sorted(project_path.rglob("*"))
        if path.is_file() and not path.name.endswith(".tmp")
    ]
    return {
        "schema_version": 1,
        "status": status,
        "created_at": datetime.now(UTC).isoformat(),
        "case": {
            "case_id": case["case_id"],
            "prompt_path": case["prompt_path"],
            "prompt_sha256": case["prompt_sha256"],
            "actor_policy_version": actor.policy_version,
        },
        "project": {
            "name": project_path.name,
            "path": str(project_path),
            "run_status": project_storage.read_project_metadata(project_path).run_status,
        },
        "profile": {
            "profile_id": profile_id,
            "model_snapshot": model_snapshot,
            "provider_snapshot": provider_snapshot,
        },
        "automated_human_gate_decisions": actor.decisions,
        "start_receipt": start_receipt,
        "terminal": terminal,
        "chapter_contract_evidence": chapter_contract_evidence,
        "run_control": run_state.model_dump(mode="json"),
        "final_readiness": readiness.model_dump(mode="json"),
        "arc_transition": {
            "first_arc": first_arc.model_dump(mode="json") if first_arc else None,
            "current_arc": current_arc.model_dump(mode="json") if current_arc else None,
        },
        "evaluations": evaluations,
        "checkpoints": checkpoints,
        "failure_retry_facts": [
            {
                "seq": event.seq,
                "kind": event.kind,
                "atomic_action": event.atomic_action,
                "status": event.status,
                "routing_decision": event.routing_decision,
                "artifact_path": event.artifact_path,
                "message": event.message,
            }
            for event in events
            if any(token in event.kind for token in ("fail", "retry", "wait", "repair"))
        ],
        "event_evidence": [
            {
                "seq": event.seq,
                "kind": event.kind,
                "loop_layer": event.loop_layer,
                "atomic_action": event.atomic_action,
                "status": event.status,
                "routing_decision": event.routing_decision,
                "artifact_path": event.artifact_path,
            }
            for event in events
            if event.kind != "llm_output_delta"
        ],
        "artifact_paths": artifacts,
        "failure": failure,
    }


def _load_case(case_path: Path) -> tuple[dict[str, object], str]:
    resolved = case_path.resolve()
    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise LiveProjectAcceptanceError("Unsupported live acceptance case schema.")
    prompt_path = ROOT_DIR / str(payload.get("prompt_path", ""))
    prompt_bytes = prompt_path.read_bytes()
    actual_hash = sha256(prompt_bytes).hexdigest()
    if actual_hash != payload.get("prompt_sha256"):
        raise LiveProjectAcceptanceError(
            "Live acceptance Prompt hash mismatch; update the versioned case deliberately."
        )
    if payload.get("operation_mode") != "participatory":
        raise LiveProjectAcceptanceError(
            "The live full-project case must use ordinary participatory mode."
        )
    return payload, prompt_bytes.decode("utf-8")


def _text_sha256(value: str | None) -> str:
    if value is None:
        raise LiveProjectAcceptanceError("Cannot hash an absent public actor decision.")
    return sha256(value.encode("utf-8")).hexdigest()


def _nested(payload: dict[str, object], first: str, second: str) -> object:
    nested = payload.get(first)
    if not isinstance(nested, dict) or second not in nested:
        raise LiveProjectAcceptanceError(
            f"Live acceptance case is missing {first}.{second}."
        )
    return nested[second]


def _failure_diagnostic(
    project_path: Path,
    redaction_values: Sequence[str],
) -> str:
    events = read_events(project_path)
    last = next((event for event in reversed(events) if event.kind != "llm_output_delta"), None)
    metadata = project_storage.read_project_metadata(project_path)
    state = read_run_control_state(project_path)
    diagnostic = {
        "project_status": metadata.run_status,
        "desired_state": state.desired_state,
        "dispatch_status": state.dispatch.status if state.dispatch else None,
        "last_event": (
            {
                "kind": last.kind,
                "atomic_action": last.atomic_action,
                "status": last.status,
                "message": last.message,
                "artifact_path": last.artifact_path,
            }
            if last is not None
            else None
        ),
        "project_path": str(project_path),
    }
    return _redact_json(diagnostic, redaction_values)


def _redact_json(value: object, redaction_values: Sequence[str]) -> str:
    return redact_sensitive_values(
        json.dumps(value, ensure_ascii=False, default=str),
        redaction_values,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the opt-in normal two-Chapter full-project acceptance with a real model."
        )
    )
    parser.add_argument("--profile-id", help="Configured real LLM profile id.")
    parser.add_argument(
        "--case",
        type=Path,
        default=DEFAULT_CASE_PATH,
        help="Versioned live acceptance case JSON.",
    )
    parser.add_argument("--skip-profile-test", action="store_true")
    parser.add_argument("--keep-active", action="store_true")
    parser.add_argument("--timeout-seconds", type=float, default=1_800)
    parser.add_argument("--poll-interval-seconds", type=float, default=0.5)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        report = run_live_project_acceptance(
            LiveProjectAcceptanceOptions(
                profile_id=args.profile_id,
                case_path=args.case,
                skip_profile_test=args.skip_profile_test,
                keep_active=args.keep_active,
                timeout_seconds=args.timeout_seconds,
                poll_interval_seconds=args.poll_interval_seconds,
            )
        )
    except LiveProjectAcceptanceError as exc:
        if args.json:
            print(
                json.dumps(
                    {"status": "failed", "message": str(exc)},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"NovelPilot live project acceptance failed: {exc}", file=sys.stderr)
        return exc.exit_code

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("NovelPilot live project acceptance passed.")
        print(f"Project: {report['project']['path']}")
        print(f"Report: {Path(report['project']['path']) / REPORT_RELATIVE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
