from __future__ import annotations

import argparse
import json
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any, Sequence


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
from app.core.config import OUTPUT_DIR  # noqa: E402
from app.llm.redaction import (  # noqa: E402
    profile_secret_values,
    redact_sensitive_values,
)
from app.llm.usage import merge_usage  # noqa: E402
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
from app.storage.run_state import (  # noqa: E402
    read_run_control_state,
    set_run_intent,
)
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


def run_live_project_acceptance_series(
    options: LiveProjectAcceptanceOptions,
    *,
    runs: int,
) -> dict[str, object]:
    if runs < 1:
        raise ValueError("Live acceptance series requires at least one run.")
    behavior_fingerprint = _behavior_fingerprint()
    reports: list[dict[str, object]] = []
    for index in range(runs):
        report = run_live_project_acceptance(options)
        if report.get("status") != "passed":
            raise LiveProjectAcceptanceError(
                f"Live acceptance series run {index + 1} did not pass."
            )
        reports.append(report)
        if _behavior_fingerprint() != behavior_fingerprint:
            raise LiveProjectAcceptanceError(
                "Behavior-affecting acceptance sources changed between consecutive runs."
            )
    prompt_hashes = {_report_prompt_hash(report) for report in reports}
    profile_snapshots = {
        json.dumps(
            _fixed_profile_snapshot(report.get("profile")),
            ensure_ascii=False,
            sort_keys=True,
        )
        for report in reports
    }
    if len(prompt_hashes) != 1 or len(profile_snapshots) != 1:
        raise LiveProjectAcceptanceError(
            "Consecutive acceptance runs did not use one fixed Prompt and profile snapshot."
        )
    token_totals = _empty_normalized_usage()
    recovery_counts: dict[str, int] = {}
    for report in reports:
        token_usage = report.get("token_usage")
        if isinstance(token_usage, dict):
            _add_normalized_usage(
                token_totals,
                cast_normalized(token_usage.get("totals")),
            )
        ledger = report.get("reset_recovery_ledger")
        counts = ledger.get("counts") if isinstance(ledger, dict) else None
        if isinstance(counts, dict):
            for category, count in counts.items():
                if isinstance(count, int):
                    recovery_counts[str(category)] = (
                        recovery_counts.get(str(category), 0) + count
                    )
    aggregate = {
        "schema_version": 1,
        "status": "passed",
        "created_at": datetime.now(UTC).isoformat(),
        "required_consecutive_passes": runs,
        "consecutive_passes": len(reports),
        "behavior_fingerprint": behavior_fingerprint,
        "prompt_sha256": next(iter(prompt_hashes)),
        "profile_snapshot_sha256": sha256(
            next(iter(profile_snapshots)).encode("utf-8")
        ).hexdigest(),
        "token_totals": token_totals,
        "recovery_counts": recovery_counts,
        "runs": [
            {
                "index": index + 1,
                "project": report.get("project"),
                "terminal": report.get("terminal"),
                "token_usage": report.get("token_usage"),
                "reset_recovery_ledger": report.get("reset_recovery_ledger"),
            }
            for index, report in enumerate(reports)
        ],
    }
    aggregate_path = (
        OUTPUT_DIR
        / "experiments"
        / "live-acceptance"
        / f"consecutive-{runs}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    write_json(aggregate_path, aggregate)
    aggregate["aggregate_report_path"] = str(aggregate_path)
    return aggregate


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
    profile_test_payload: dict[str, object] | None = None
    host = get_run_host()
    host_started_here = not host.started

    try:
        _assert_no_competing_runnable_projects()
        project = projects_api.create_project(
            CreateProjectRequest(
                operation_mode="participatory",
                project_kind="benchmark_mother",
            )
        )
        project_path = Path(project.path)
        profiles_api.select_profile(profile.id)
        profile_test = _test_profile(
            profile.id,
            options.skip_profile_test,
            redaction_values,
        )
        profile_test_payload = profile_test.model_dump(mode="json")
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
            profile_test=profile_test_payload,
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
                profile_test=profile_test_payload,
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
                profile_test=profile_test_payload,
                start_receipt=start_receipt,
                terminal=None,
                chapter_contract_evidence=None,
                failure=str(error),
            )
            write_json(project_path / REPORT_RELATIVE, report)
        raise error from exc
    finally:
        if project_path is not None:
            _request_acceptance_project_stop(project_path)
        if host_started_here and host.started:
            host.stop(timeout=options.timeout_seconds)
        if project_path is not None and not host.started:
            _finalize_acceptance_project_stop(project_path)
        if not options.keep_active:
            _restore_runtime_state(previous_project_path, previous_profile_id)


def _request_acceptance_project_stop(project_path: Path) -> None:
    """Durably remove an isolated acceptance project from the RunHost queue."""

    set_run_intent(
        project_path,
        desired_state="stopped",
        clear_provider_wait=True,
    )


def _finalize_acceptance_project_stop(project_path: Path) -> None:
    """Leave a timed-out test project visibly paused after its local host stops."""

    state = read_run_control_state(project_path)
    if state.desired_state != "stopped":
        return
    with project_storage.project_metadata_lock(project_path):
        metadata = project_storage.read_project_metadata(project_path)
        if metadata.run_status in {
            "running",
            "pause_requested",
            "waiting_for_provider",
        }:
            metadata.run_status = "paused"
            project_storage.write_project_metadata(project_path, metadata)


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
            selected_title = state.selected_title
            actor.record_book_approval(state)
            approved = _call_user_action(
                "approve the exact reviewed Book candidate",
                lambda: setup_api.approve_setup(
                    SetupApprovalRequest(
                        candidate_revision=candidate.revision,
                        title=selected_title,
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
    expected_target = _required_int(
        _nested(case, "first_arc", "expected_target_chapter_count")
    )
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
            if current_arc.arc_id not in {"arc-001", "arc-002"}:
                raise LiveProjectAcceptanceError(
                    "An unexpected Story Arc approval gate appeared in the benchmark flow: "
                    f"checkpoint: arc={current_arc.arc_id}."
                )
            if current_arc.arc_id == "arc-001" and first_arc_approved:
                raise LiveProjectAcceptanceError("Arc 1 approval was requested twice.")
            if current_arc.arc_id == "arc-002" and not first_arc_approved:
                raise LiveProjectAcceptanceError("Arc 2 appeared before Arc 1 approval.")
            recommended = current_arc.recommended_target_chapter_count
            if current_arc.arc_id == "arc-001" and recommended != expected_target:
                raise LiveProjectAcceptanceError(
                    "The model recommendation did not honor the two-Chapter case: "
                    f"recommended={recommended}, expected={expected_target}."
                )
            actor.record_arc_approval(
                arc_id=current_arc.arc_id,
                recommended_target_chapter_count=recommended,
            )
            approval = _call_user_action(
                "approve the exact recommended Story Arc target",
                lambda: arcs_api.approve_current_arc(
                    CurrentArcApprovalRequest(target_chapter_count=recommended)
                ),
                redaction_values,
            )
            actor.record_result(gate="story_arc_approval", project_path=project_path)
            if current_arc.arc_id == "arc-001":
                first_arc_approved = True
                continue
            transition = approval.fixture_transition
            if transition is None or transition.status != "frozen":
                raise LiveProjectAcceptanceError(
                    "Arc 2 approval did not automatically freeze the benchmark mother: "
                    f"transition={transition!r}."
                )
            metadata = project_storage.read_project_metadata(project_path)
            return {
                "project_status": metadata.run_status,
                "readiness_action": readiness.next_action.id,
                "active_arc_id": metadata.active_arc_id,
                "event_count": len(read_events(project_path)),
                "benchmark_fixture_status": transition.status,
                "fixture_id": (
                    transition.fixture.fixture_id
                    if transition.fixture is not None
                    else None
                ),
            }

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
    metadata = project_storage.read_project_metadata(project_path)
    if metadata.project_kind == "benchmark_mother":
        return bool(
            metadata.benchmark_fixture is not None
            and metadata.benchmark_fixture.status == "frozen"
        )
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
        and current_arc.human_review in {"awaiting_review", "approved"}
        and readiness_action in {"approve_story_arc", "inspect_fixture"}
        and all(path.is_file() for path in finals)
    )


def _normalized_fact_text(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _missing_stable_facts(
    prose: str,
    required: dict[str, tuple[str, ...]],
) -> list[str]:
    normalized_prose = _normalized_fact_text(prose)
    return [
        fact_id
        for fact_id, representations in required.items()
        if not any(
            _normalized_fact_text(representation) in normalized_prose
            for representation in representations
        )
    ]


def _assert_two_chapter_contract(
    project_path: Path,
    case: dict[str, object],
) -> dict[str, object]:
    metadata = project_storage.read_project_metadata(project_path)
    if (
        metadata.project_kind != "benchmark_mother"
        or metadata.benchmark_fixture is None
        or metadata.benchmark_fixture.status != "frozen"
    ):
        raise LiveProjectAcceptanceError(
            "The two-Chapter acceptance did not finish as a frozen benchmark mother."
        )
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
    required_chapter_1 = {
        "林澈": ("林澈",),
        "许青": ("许青",),
        "23:17": ("23:17", "二十三点十七分", "二十三时十七分"),
        "0417": ("0417",),
    }
    required_chapter_2 = required_chapter_1
    absent = _missing_stable_facts(chapter_1, required_chapter_1)
    absent.extend(_missing_stable_facts(chapter_2, required_chapter_2))
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
    if _required_int(_nested(case, "first_arc", "expected_target_chapter_count")) != 2:
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
        "benchmark_fixture_status": metadata.benchmark_fixture.status,
        "benchmark_fixture_id": metadata.benchmark_fixture.fixture_id,
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


def _build_model_usage_report(
    project_path: Path,
    profile_test: dict[str, object] | None,
) -> dict[str, object]:
    entries: list[dict[str, object]] = []

    def add_entry(
        *,
        role: str,
        loop: str,
        phase: str,
        call_type: str,
        calls: int,
        usage: object,
        usage_available: bool,
        artifact_path: str | None,
    ) -> None:
        raw_usage = usage if isinstance(usage, dict) else {}
        entries.append(
            {
                "role": role,
                "loop": loop,
                "phase": phase,
                "call_type": call_type,
                "calls": calls,
                "usage": raw_usage,
                "normalized_tokens": _normalized_usage(raw_usage),
                "usage_available": usage_available and bool(raw_usage),
                "artifact_path": artifact_path,
            }
        )

    for path in sorted(project_path.rglob("telemetry.json")):
        payload = read_json(path, default={})
        if not isinstance(payload, dict) or "activation_id" not in payload:
            continue
        role = str(payload.get("role", _role_from_path(project_path, path)))
        add_entry(
            role=role,
            loop=role,
            phase=str(payload.get("phase", "unknown")),
            call_type="agent_turn",
            calls=_required_int(payload.get("llm_calls", 0)),
            usage=payload.get("usage"),
            usage_available=bool(payload.get("usage")),
            artifact_path=path.relative_to(project_path).as_posix(),
        )

    for path in sorted(project_path.rglob("evaluation.json")):
        payload = read_json(path, default={})
        if not isinstance(payload, dict):
            continue
        telemetry = payload.get("telemetry")
        role = _role_from_path(project_path, path)
        if not isinstance(telemetry, dict):
            add_entry(
                role="evaluator",
                loop=role,
                phase=str(payload.get("evaluation_mode", "unknown")),
                call_type="evaluator_unavailable",
                calls=0,
                usage={},
                usage_available=False,
                artifact_path=path.relative_to(project_path).as_posix(),
            )
            continue
        attempts = telemetry.get("attempts")
        if not isinstance(attempts, list):
            attempts = []
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            add_entry(
                role="evaluator",
                loop=role,
                phase=str(payload.get("evaluation_mode", "unknown")),
                call_type="evaluator_" + str(attempt.get("call_type", "unknown")),
                calls=1,
                usage=attempt.get("usage"),
                usage_available=bool(attempt.get("usage_available")),
                artifact_path=path.relative_to(project_path).as_posix(),
            )

    profile_calls = profile_test.get("calls") if profile_test is not None else None
    if isinstance(profile_calls, list) and profile_calls:
        for call in profile_calls:
            if not isinstance(call, dict):
                continue
            add_entry(
                role="profile_capability_test",
                loop="profile",
                phase="capability_test",
                call_type=str(call.get("call_type", "unknown")),
                calls=1,
                usage=call.get("usage"),
                usage_available=bool(call.get("usage_available")),
                artifact_path=None,
            )
    else:
        add_entry(
            role="profile_capability_test",
            loop="profile",
            phase="capability_test",
            call_type="cached_or_unavailable",
            calls=0,
            usage=(profile_test or {}).get("usage", {}),
            usage_available=bool((profile_test or {}).get("usage_available")),
            artifact_path=None,
        )

    grouped: dict[str, dict[str, object]] = {}
    totals = _empty_normalized_usage()
    unavailable_entries = 0
    for entry in entries:
        key = "|".join(
            str(entry[field])
            for field in ("role", "loop", "phase", "call_type")
        )
        group = grouped.setdefault(
            key,
            {
                "role": entry["role"],
                "loop": entry["loop"],
                "phase": entry["phase"],
                "call_type": entry["call_type"],
                "calls": 0,
                "usage": {},
                "normalized_tokens": _empty_normalized_usage(),
                "usage_available": True,
            },
        )
        group["calls"] = _required_int(group["calls"]) + _required_int(entry["calls"])
        group["usage"] = merge_usage(
            cast_usage(group["usage"]),
            cast_usage(entry["usage"]),
        )
        _add_normalized_usage(
            cast_normalized(group["normalized_tokens"]),
            cast_normalized(entry["normalized_tokens"]),
        )
        if not bool(entry["usage_available"]):
            group["usage_available"] = False
            unavailable_entries += 1
        _add_normalized_usage(totals, cast_normalized(entry["normalized_tokens"]))
    return {
        "schema_version": 1,
        "totals": totals,
        "usage_complete": unavailable_entries == 0,
        "unavailable_entry_count": unavailable_entries,
        "by_role_loop_phase_call_type": list(grouped.values()),
        "entries": entries,
    }


def _build_recovery_ledger(
    project_path: Path,
    *,
    checkpoints: list[object],
    profile_test: dict[str, object] | None = None,
) -> dict[str, object]:
    entries: list[dict[str, object]] = []
    profile_calls = profile_test.get("calls") if profile_test is not None else None
    if isinstance(profile_calls, list):
        for call in profile_calls:
            if not isinstance(call, dict):
                continue
            retries = _required_int(call.get("transport_retries", 0))
            if retries:
                entries.append(
                    _recovery_entry(
                        category="transport_retry",
                        reason_code="profile_capability_transport_provider_retry",
                        trigger="system",
                        source_checkpoint=str(call.get("call_type", "unknown")),
                        target_checkpoint=str(call.get("call_type", "unknown")),
                        model_reinvoked=True,
                        extra_tokens=None,
                        result="passed",
                        count=retries,
                        evidence_path="profile-capability-test",
                    )
                )
    telemetry_by_activation: dict[str, tuple[str, dict[str, object]]] = {}
    activations_by_run: dict[str, list[tuple[str, dict[str, object]]]] = {}
    for path in sorted(project_path.rglob("telemetry.json")):
        payload = read_json(path, default={})
        if not isinstance(payload, dict) or not isinstance(payload.get("activation_id"), str):
            continue
        relative = path.relative_to(project_path).as_posix()
        activation_id = str(payload["activation_id"])
        telemetry_by_activation[activation_id] = (relative, payload)
        candidate_run_id = str(payload.get("candidate_run_id", "unknown"))
        activations_by_run.setdefault(candidate_run_id, []).append((relative, payload))
        tool_repairs = _required_int(payload.get("activation_tool_schema_repairs", 0))
        if tool_repairs:
            entries.append(
                _recovery_entry(
                    category="tool_schema_repair",
                    reason_code="recoverable_tool_validation",
                    trigger="system",
                    source_checkpoint=activation_id,
                    target_checkpoint=activation_id,
                    model_reinvoked=True,
                    extra_tokens=None,
                    result=str(payload.get("outcome", "unknown")),
                    count=tool_repairs,
                    evidence_path=relative,
                )
            )
        transport_retries = _required_int(
            payload.get("activation_transport_retries", 0)
        )
        if transport_retries:
            entries.append(
                _recovery_entry(
                    category="transport_retry",
                    reason_code="agent_transport_provider_retry",
                    trigger="system",
                    source_checkpoint=activation_id,
                    target_checkpoint=activation_id,
                    model_reinvoked=True,
                    extra_tokens=None,
                    result=str(payload.get("outcome", "unknown")),
                    count=transport_retries,
                    evidence_path=relative,
                )
            )

    for candidate_run_id, activations in activations_by_run.items():
        ordered = sorted(activations, key=lambda item: str(item[1].get("started_at", "")))
        for (source_path, source), (target_path, target) in zip(ordered, ordered[1:]):
            entries.append(
                _recovery_entry(
                    category="activation_restart_resume",
                    reason_code=(
                        "semantic_repair_activation"
                        if _required_int(
                            cast_usage(target.get("candidate_budgets")).get(
                                "used_semantic_revisions", 0
                            )
                        )
                        else "candidate_activation_resume"
                    ),
                    trigger="system",
                    source_checkpoint=str(source.get("activation_id")),
                    target_checkpoint=str(target.get("activation_id")),
                    model_reinvoked=_required_int(target.get("llm_calls", 0)) > 0,
                    extra_tokens=_normalized_usage(cast_usage(target.get("usage")))[
                        "total_tokens"
                    ],
                    result=str(target.get("outcome", "unknown")),
                    count=1,
                    evidence_path=target_path,
                    candidate_run_id=candidate_run_id,
                    source_evidence_path=source_path,
                )
            )

    for path in sorted(project_path.rglob("repair-chain.json")):
        chain = read_json(path, default={})
        chain_entries = chain.get("entries") if isinstance(chain, dict) else None
        if not isinstance(chain_entries, list):
            continue
        for source, target in zip(chain_entries, chain_entries[1:]):
            if not isinstance(source, dict) or not isinstance(target, dict):
                continue
            activation_id = str(target.get("activation_id", ""))
            telemetry = telemetry_by_activation.get(activation_id)
            extra_tokens = (
                _normalized_usage(cast_usage(telemetry[1].get("usage")))["total_tokens"]
                if telemetry is not None
                else None
            )
            entries.append(
                _recovery_entry(
                    category="semantic_revision",
                    reason_code="evaluator_local_repair",
                    trigger="system",
                    source_checkpoint=str(source.get("candidate_artifact_id")),
                    target_checkpoint=str(target.get("candidate_artifact_id")),
                    model_reinvoked=telemetry is not None
                    and _required_int(telemetry[1].get("llm_calls", 0)) > 0,
                    extra_tokens=extra_tokens,
                    result=(
                        "passed"
                        if not target.get("open_issue_ids")
                        else "continued_with_open_issues"
                    ),
                    count=1,
                    evidence_path=path.relative_to(project_path).as_posix(),
                    candidate_run_id=str(chain.get("candidate_run_id")),
                )
            )

    for path in sorted(project_path.rglob("evaluation.json")):
        payload = read_json(path, default={})
        telemetry = payload.get("telemetry") if isinstance(payload, dict) else None
        retries = (
            _required_int(telemetry.get("transport_retries", 0))
            if isinstance(telemetry, dict)
            else 0
        )
        if retries:
            entries.append(
                _recovery_entry(
                    category="transport_retry",
                    reason_code="evaluator_transport_provider_retry",
                    trigger="system",
                    source_checkpoint=str(payload.get("candidate_artifact_id")),
                    target_checkpoint=str(payload.get("evaluation_id")),
                    model_reinvoked=True,
                    extra_tokens=None,
                    result=str(
                        cast_usage(payload.get("result")).get("outcome", "unknown")
                    ),
                    count=retries,
                    evidence_path=path.relative_to(project_path).as_posix(),
                )
            )

    checkpoint_groups: dict[str, list[dict[str, object]]] = {}
    for checkpoint in checkpoints:
        if not isinstance(checkpoint, dict):
            continue
        key = str(checkpoint.get("action_key", ""))
        if key:
            checkpoint_groups.setdefault(key, []).append(checkpoint)
    for action_key, group in checkpoint_groups.items():
        if len(group) < 2:
            continue
        entries.append(
            _recovery_entry(
                category="checkpoint_replay_reset",
                reason_code="repeated_action_checkpoint",
                trigger="system",
                source_checkpoint=str(group[0].get("checkpoint_id")),
                target_checkpoint=str(group[-1].get("checkpoint_id")),
                model_reinvoked=any(item.get("candidate_run_id") for item in group[1:]),
                extra_tokens=None,
                result=str(group[-1].get("status", "unknown")),
                count=len(group) - 1,
                evidence_path=f"book/harness/checkpoints#{action_key}",
            )
        )

    events = read_events(project_path)
    for event in events:
        if event.kind not in {"run_resumed", "run_recovered", "run_host_reconciled"}:
            continue
        entries.append(
            _recovery_entry(
                category=(
                    "run_resume" if event.kind == "run_resumed" else "checkpoint_replay_reset"
                ),
                reason_code=event.kind,
                trigger="system",
                source_checkpoint=event.atomic_action,
                target_checkpoint=event.artifact_path,
                model_reinvoked=None,
                extra_tokens=None,
                result=event.status,
                count=1,
                evidence_path=f"events.jsonl#{event.seq}",
            )
        )

    categories = (
        "transport_retry",
        "tool_schema_repair",
        "semantic_revision",
        "activation_restart_resume",
        "checkpoint_replay_reset",
        "run_resume",
        "full_project_restart",
    )
    return {
        "schema_version": 1,
        "counts": {
            category: sum(
                _required_int(item["count"])
                for item in entries
                if item["category"] == category
            )
            for category in categories
        },
        "entries": entries,
    }


def _recovery_entry(
    *,
    category: str,
    reason_code: str,
    trigger: str,
    source_checkpoint: str | None,
    target_checkpoint: str | None,
    model_reinvoked: bool | None,
    extra_tokens: int | None,
    result: str | None,
    count: int,
    evidence_path: str,
    **extra: object,
) -> dict[str, object]:
    return {
        "category": category,
        "reason_code": reason_code,
        "trigger": trigger,
        "source_checkpoint": source_checkpoint,
        "target_checkpoint": target_checkpoint,
        "model_reinvoked": model_reinvoked,
        "extra_tokens": extra_tokens,
        "extra_tokens_available": extra_tokens is not None,
        "result": result,
        "count": count,
        "evidence_path": evidence_path,
        **extra,
    }


def _role_from_path(project_path: Path, path: Path) -> str:
    parts = path.relative_to(project_path).parts
    if parts and parts[0] == "book":
        return "book"
    if parts and parts[0] == "arcs":
        return "story_arc"
    if parts and parts[0] == "chapters":
        return "chapter"
    return "unknown"


def _normalized_usage(usage: dict[str, Any]) -> dict[str, int]:
    prompt = _usage_number(usage, "prompt_tokens", "input_tokens")
    completion = _usage_number(usage, "completion_tokens", "output_tokens")
    cached = _usage_number(usage, "cached_tokens", "cache_read_input_tokens")
    prompt_details = usage.get("prompt_tokens_details")
    if cached == 0 and isinstance(prompt_details, dict):
        cached = _usage_number(prompt_details, "cached_tokens")
    total = _usage_number(usage, "total_tokens")
    if total == 0 and (prompt or completion):
        total = prompt + completion
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cached_tokens": cached,
        "total_tokens": total,
    }


def _usage_number(usage: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = usage.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
    return 0


def _empty_normalized_usage() -> dict[str, int]:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cached_tokens": 0,
        "total_tokens": 0,
    }


def _add_normalized_usage(
    target: dict[str, int],
    current: dict[str, int],
) -> None:
    for key in target:
        target[key] += current.get(key, 0)


def cast_usage(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def cast_normalized(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return _empty_normalized_usage()
    return {
        key: int(item) if isinstance(item, (int, float)) else 0
        for key, item in value.items()
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
    profile_test: dict[str, object] | None,
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
                "telemetry": payload.get("telemetry"),
            }
        )
    token_usage = _build_model_usage_report(project_path, profile_test)
    recovery_ledger = _build_recovery_ledger(
        project_path,
        checkpoints=checkpoints,
        profile_test=profile_test,
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
        "schema_version": 2,
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
            "capability_test": profile_test,
        },
        "token_usage": token_usage,
        "reset_recovery_ledger": recovery_ledger,
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


def _behavior_fingerprint() -> str:
    paths = [
        *sorted((BACKEND_DIR / "app").rglob("*.py")),
        Path(__file__).resolve(),
        DEFAULT_CASE_PATH,
    ]
    case = json.loads(DEFAULT_CASE_PATH.read_text(encoding="utf-8"))
    prompt_path = ROOT_DIR / str(case.get("prompt_path", ""))
    if prompt_path.is_file():
        paths.append(prompt_path)
    digest = sha256()
    for path in paths:
        relative = path.relative_to(ROOT_DIR).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _fixed_profile_snapshot(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    capability_result = value.get("capability_test")
    capability = (
        capability_result.get("capability_test")
        if isinstance(capability_result, dict)
        else None
    )
    return {
        "profile_id": value.get("profile_id"),
        "model_snapshot": value.get("model_snapshot"),
        "provider_snapshot": value.get("provider_snapshot"),
        "profile_fingerprint": (
            capability.get("profile_fingerprint")
            if isinstance(capability, dict)
            else None
        ),
    }


def _report_prompt_hash(report: dict[str, object]) -> str:
    case = report.get("case")
    return str(case.get("prompt_sha256")) if isinstance(case, dict) else ""


def _required_int(value: object) -> int:
    if not isinstance(value, (int, float, str)) or isinstance(value, bool):
        raise LiveProjectAcceptanceError(f"Expected numeric acceptance value, got {value!r}.")
    return int(value)


def _single_project_path(report: dict[str, object]) -> str:
    project = report.get("project")
    path = project.get("path") if isinstance(project, dict) else None
    if not isinstance(path, str):
        raise LiveProjectAcceptanceError("Acceptance report is missing its project path.")
    return path


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
    parser.add_argument(
        "--runs",
        type=int,
        default=1,
        help="Number of consecutive isolated benchmark-mother runs.",
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        options = LiveProjectAcceptanceOptions(
                profile_id=args.profile_id,
                case_path=args.case,
                skip_profile_test=args.skip_profile_test,
                keep_active=args.keep_active,
                timeout_seconds=args.timeout_seconds,
                poll_interval_seconds=args.poll_interval_seconds,
            )
        report = (
            run_live_project_acceptance_series(options, runs=args.runs)
            if args.runs > 1
            else run_live_project_acceptance(options)
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
        if args.runs > 1:
            print(
                "NovelPilot live project acceptance series passed: "
                f"{report['consecutive_passes']}/{report['required_consecutive_passes']}."
            )
            print(f"Aggregate report: {report['aggregate_report_path']}")
        else:
            print("NovelPilot live project acceptance passed.")
            project_path = _single_project_path(report)
            print(f"Project: {project_path}")
            print(f"Report: {Path(project_path) / REPORT_RELATIVE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
