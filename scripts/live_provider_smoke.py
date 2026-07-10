from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence, TypeVar


ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi import HTTPException  # noqa: E402

from app.api import exports as exports_api  # noqa: E402
from app.api import profiles as profiles_api  # noqa: E402
from app.api import projects as projects_api  # noqa: E402
from app.api import runs as runs_api  # noqa: E402
from app.api import setup as setup_api  # noqa: E402
from app.llm.redaction import profile_secret_values, redact_sensitive_values  # noqa: E402
from app.schemas.profiles import LlmProfile, LlmProfileTestResult  # noqa: E402
from app.schemas.projects import CreateProjectRequest  # noqa: E402
from app.schemas.runs import RunAdvanceRequest  # noqa: E402
from app.schemas.setup import (  # noqa: E402
    SetupApprovalRequest,
    SetupStateDocument,
    SetupTurnRequest,
)
from app.storage import profiles as profile_storage  # noqa: E402
from app.storage import projects as project_storage  # noqa: E402
from app.storage.events import read_events  # noqa: E402
from app.storage.json_files import read_json, write_json  # noqa: E402
from app.storage.secret_audit import audit_path_for_profile_secrets  # noqa: E402


REQUIRED_ARTIFACTS = {
    "book_direction": "book/direction.md",
    "book_constraints": "book/constraints.json",
    "book_rolling_contract": "book/outline.md",
    "book_discussion_transcript": "book/discussion/transcript.jsonl",
    "context_snapshot": "chapters/chapter-001/context_snapshot.json",
    "goal": "chapters/chapter-001/goal.md",
    "draft": "chapters/chapter-001/draft.md",
    "observations": "chapters/chapter-001/observations.json",
    "review": "chapters/chapter-001/review.md",
    "verification": "chapters/chapter-001/verification.json",
    "final": "chapters/chapter-001/final.md",
    "candidate_state_patch": "chapters/chapter-001/candidate_state_patch.json",
    "committed_state_patch": "chapters/chapter-001/committed_state_patch.json",
}
T = TypeVar("T")

SMOKE_BOOK_DIRECTION_BRIEF = """请基于以下意图开始全书方向讨论，并维护一份完整、具体的 Book Direction 草稿：

- 作品是一部紧凑的近未来海滨城市悬疑冒险，线索必须公平可见，反转要同时改变人物关系。
- 主角聪明但孤立，长期变化是学会辨认值得信任的盟友，并为主动权付出真实代价。
- 推想技术有限、有社会后果，不能充当任意解题工具；世界保持现实因果。
- 每个故事弧应兑现一次策略发现、一次情感转折，并留下由已提交正史自然产生的新问题。
- 长期结局倾向是艰难但有希望：主角获得能动性和共同体，同时保留不可抹去的损失。
- 禁止提前列出未来全部故事弧或章节，只为后续滚动规划保留稳定方向和边界。

这些是我明确确认的创作决定。具体的局部反派、每个故事弧路线和最终损失对象保持开放。你可以指出真正影响创作的矛盾或缺口，但不要把覆盖清单变成固定问卷。"""


class LiveProviderSmokeError(RuntimeError):
    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class LiveProviderSmokeOptions:
    profile_id: str | None
    title: str | None = None
    skip_profile_test: bool = False
    keep_active: bool = False


@dataclass(frozen=True)
class LiveProviderSmokeResult:
    status: str
    project_name: str
    project_path: str
    profile_id: str
    model_snapshot: str
    provider_snapshot: str
    run_status: str
    event_count: int
    artifacts: dict[str, str]
    manual_review_paths: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "project_name": self.project_name,
            "project_path": self.project_path,
            "profile_id": self.profile_id,
            "model_snapshot": self.model_snapshot,
            "provider_snapshot": self.provider_snapshot,
            "run_status": self.run_status,
            "event_count": self.event_count,
            "artifacts": self.artifacts,
            "manual_review_paths": self.manual_review_paths,
        }


@dataclass(frozen=True)
class LiveProviderSmokeFailureReport:
    status: str
    project_name: str
    project_path: str
    profile_id: str
    model_snapshot: str
    provider_snapshot: str
    run_status: str
    event_count: int
    artifacts: dict[str, str]
    manual_review_paths: list[str]
    failure: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "project_name": self.project_name,
            "project_path": self.project_path,
            "profile_id": self.profile_id,
            "model_snapshot": self.model_snapshot,
            "provider_snapshot": self.provider_snapshot,
            "run_status": self.run_status,
            "event_count": self.event_count,
            "artifacts": self.artifacts,
            "manual_review_paths": self.manual_review_paths,
            "failure": self.failure,
        }


def run_smoke(options: LiveProviderSmokeOptions) -> LiveProviderSmokeResult:
    previous_project_path = project_storage.get_active_project_path()
    previous_profile_id = profile_storage.load_profiles().active_profile_id
    profile_id = _select_profile_id(options.profile_id)
    profile = _load_enabled_profile(profile_id)
    redaction_values = profile_secret_values(profile)
    project_path: Path | None = None

    try:
        project = projects_api.create_project(
            CreateProjectRequest(
                title=options.title or _default_project_title(),
                operation_mode="full_auto",
            )
        )
        project_path = Path(project.path)
        profiles_api.select_profile(profile.id)
        profile_test = _test_profile(profile.id, options.skip_profile_test, redaction_values)

        setup_state = _complete_book_setup(redaction_values)
        if not setup_state.approved:
            raise LiveProviderSmokeError("Book setup did not reach approved state.")

        run_result = _call_user_action(
            "start harness run",
            lambda: runs_api.start_run(RunAdvanceRequest(stop_after_chapter=True)),
            redaction_values,
        )
        _assert_required_artifacts(project_path, redaction_values)
        export_result = _call_user_action(
            "export manuscript",
            exports_api.export_current_manuscript,
            redaction_values,
        )
        artifacts = _artifact_map(project_path, str(export_result["artifact_path"]))
        _assert_no_secret_leak(project_path, profile, redaction_values)

        metadata = project_storage.read_project_metadata(project_path)
        if run_result["status"] != "idle" or metadata.run_status != "idle":
            raise LiveProviderSmokeError(
                "Harness did not finish at an idle checkpoint. "
                f"run_result={run_result}; project={project_path}"
                + _smoke_failure_context(project_path, redaction_values)
            )

        events = read_events(project_path)
        if not any(event.kind == "state_patch_committed" for event in events):
            raise LiveProviderSmokeError(
                "Harness did not commit a state patch."
                + _smoke_failure_context(project_path, redaction_values)
            )

        result = LiveProviderSmokeResult(
            status="passed",
            project_name=project.name,
            project_path=str(project_path),
            profile_id=profile.id,
            model_snapshot=profile_test.model_snapshot,
            provider_snapshot=profile_test.provider_snapshot,
            run_status=metadata.run_status,
            event_count=len(events),
            artifacts=artifacts,
            manual_review_paths=[
                artifacts["final"],
                artifacts["review"],
                artifacts["verification"],
                artifacts["candidate_state_patch"],
                artifacts["committed_state_patch"],
            ],
        )
        _write_smoke_report(project_path, result)
        return result
    except LiveProviderSmokeError as exc:
        if project_path is not None:
            _write_failed_smoke_report(project_path, profile, exc, redaction_values)
        raise
    except Exception as exc:
        smoke_error = LiveProviderSmokeError(
            "Unexpected live provider smoke failure: "
            + redact_sensitive_values(str(exc), redaction_values)
        )
        if project_path is not None:
            _write_failed_smoke_report(project_path, profile, smoke_error, redaction_values)
        raise smoke_error from exc
    finally:
        if not options.keep_active:
            _restore_runtime_state(previous_project_path, previous_profile_id)


def _complete_book_setup(redaction_values: Sequence[str]) -> SetupStateDocument:
    setup_state = _call_user_action(
        "read book setup state",
        setup_api.get_setup_state,
        redaction_values,
    )
    if setup_state.approved:
        return setup_state

    next_message = SMOKE_BOOK_DIRECTION_BRIEF
    for review_attempt in range(1, 4):
        setup_state = _call_user_action(
            "continue book direction discussion",
            lambda message=next_message: setup_api.continue_setup_discussion(
                SetupTurnRequest(message=message)
            ),
            redaction_values,
        )
        setup_state = _call_user_action(
            "prepare book direction review",
            setup_api.prepare_setup_review,
            redaction_values,
        )
        candidate = setup_state.candidate
        if candidate is None:
            raise LiveProviderSmokeError("Book direction review produced no candidate.")
        if candidate.approval_allowed:
            return _call_user_action(
                "approve reviewed book direction",
                lambda revision=candidate.revision: setup_api.approve_setup(
                    SetupApprovalRequest(candidate_revision=revision)
                ),
                redaction_values,
            )

        blocking_issues = [
            issue for issue in candidate.review.issues if issue.severity == "blocking"
        ]
        issue_text = "\n".join(
            f"- {issue.message}"
            + (f" 建议澄清：{issue.suggested_question}" if issue.suggested_question else "")
            for issue in blocking_issues
        )
        next_message = (
            f"第 {review_attempt} 次候选审阅发现以下阻断问题，请据此继续讨论并修订完整草稿。"
            "不要替我改变已经确认的决定；如果问题只是表达过薄，请把现有决定具体化。\n"
            + issue_text
        )

    raise LiveProviderSmokeError(
        "Book direction remained blocked after three synthesis/review attempts."
    )


def _call_user_action(
    action: str,
    callback: Callable[[], T],
    redaction_values: Sequence[str],
) -> T:
    try:
        return callback()
    except HTTPException as exc:
        detail = _redacted_error_detail(exc.detail, redaction_values)
        raise LiveProviderSmokeError(f"Failed to {action}: {detail}") from exc
    except (RuntimeError, ValueError) as exc:
        detail = redact_sensitive_values(str(exc), redaction_values)
        raise LiveProviderSmokeError(f"Failed to {action}: {detail}") from exc


def _redacted_error_detail(detail: object, redaction_values: Sequence[str]) -> str:
    if isinstance(detail, str):
        rendered = detail
    else:
        rendered = json.dumps(
            detail,
            ensure_ascii=False,
            default=str,
        )
    return redact_sensitive_values(rendered, redaction_values)


def _select_profile_id(requested_profile_id: str | None) -> str:
    document = profile_storage.load_profiles()
    profile_id = requested_profile_id or document.active_profile_id
    if profile_id is None:
        raise LiveProviderSmokeError(
            "No active LLM profile is configured. Add one in config/llm-profiles.local.json "
            "or the frontend LLM Profiles panel, then rerun this command.",
            exit_code=2,
        )
    return profile_id


def _load_enabled_profile(profile_id: str):
    try:
        profile = profile_storage.get_profile(profile_id)
    except KeyError as exc:
        raise LiveProviderSmokeError(f"Profile not found: {profile_id}", exit_code=2) from exc
    if not profile.enabled:
        raise LiveProviderSmokeError(f"Profile is disabled: {profile_id}", exit_code=2)
    return profile


def _test_profile(
    profile_id: str,
    skip_profile_test: bool,
    redaction_values: Sequence[str],
) -> LlmProfileTestResult:
    if skip_profile_test:
        profile = profile_storage.get_profile(profile_id)
        return LlmProfileTestResult(
            profile_id=profile.id,
            ok=True,
            model_snapshot=profile.model,
            provider_snapshot=profile.protocol,
            message="Profile test skipped by CLI flag.",
        )
    return _call_user_action(
        "test LLM profile",
        lambda: profiles_api.test_profile(profile_id),
        redaction_values,
    )


def _assert_required_artifacts(project_path: Path, redaction_values: Sequence[str]) -> None:
    missing = [
        relative_path
        for relative_path in REQUIRED_ARTIFACTS.values()
        if not (project_path / relative_path).exists()
    ]
    if missing:
        metadata = project_storage.read_project_metadata(project_path)
        raise LiveProviderSmokeError(
            "Live provider flow did not produce all required chapter artifacts. "
            f"missing={missing}; run_status={metadata.run_status}; project={project_path}"
            + _smoke_failure_context(project_path, redaction_values)
        )


def _artifact_map(project_path: Path, manuscript_path: str) -> dict[str, str]:
    artifacts = {name: str(project_path / relative) for name, relative in REQUIRED_ARTIFACTS.items()}
    artifacts["manuscript"] = str(project_path / manuscript_path)
    artifacts["smoke_report"] = str(project_path / "exports" / "live_smoke_report.json")
    artifacts["literary_review"] = str(project_path / "exports" / "literary_review.json")
    return artifacts


def _expected_artifact_map(project_path: Path) -> dict[str, str]:
    return _artifact_map(project_path, "exports/manuscript.md")


def _assert_no_secret_leak(
    project_path: Path,
    profile: LlmProfile,
    redaction_values: Sequence[str],
) -> None:
    audit = audit_path_for_profile_secrets(project_path, [profile])
    if audit.findings:
        findings = ", ".join(
            f"{finding.path} ({finding.kind})"
            for finding in audit.findings
        )
        raise LiveProviderSmokeError(
            "Live provider smoke detected provider secret/config leakage in novel output: "
            + findings
            + _smoke_failure_context(project_path, redaction_values)
        )


def _write_smoke_report(project_path: Path, result: LiveProviderSmokeResult) -> None:
    report_path = project_path / "exports" / "live_smoke_report.json"
    payload = result.to_dict()
    payload["created_at"] = datetime.now(UTC).isoformat()
    write_json(report_path, payload)


def _write_failed_smoke_report(
    project_path: Path,
    profile: LlmProfile,
    exc: LiveProviderSmokeError,
    redaction_values: Sequence[str],
) -> None:
    try:
        metadata = project_storage.read_project_metadata(project_path)
        run_status = metadata.run_status
    except (FileNotFoundError, ValueError, json.JSONDecodeError):
        run_status = "unknown"
    events = read_events(project_path) if project_path.exists() else []
    report = LiveProviderSmokeFailureReport(
        status="failed",
        project_name=project_path.name,
        project_path=str(project_path),
        profile_id=profile.id,
        model_snapshot=profile.model,
        provider_snapshot=profile.protocol,
        run_status=run_status,
        event_count=len(events),
        artifacts=_expected_artifact_map(project_path),
        manual_review_paths=[],
        failure=_smoke_failure_diagnostics(
            project_path,
            redact_sensitive_values(str(exc), redaction_values),
            redaction_values,
        ),
    )
    report_path = project_path / "exports" / "live_smoke_report.json"
    payload = report.to_dict()
    payload["created_at"] = datetime.now(UTC).isoformat()
    write_json(report_path, payload)


def _smoke_failure_context(project_path: Path, redaction_values: Sequence[str]) -> str:
    diagnostics = _smoke_failure_diagnostics(project_path, None, redaction_values)
    return _render_failure_diagnostics(diagnostics)


def _smoke_failure_diagnostics(
    project_path: Path,
    message: str | None,
    redaction_values: Sequence[str],
) -> dict[str, object]:
    if not project_path.exists():
        return {"message": message or "", "project_path": str(project_path)}

    diagnostics: dict[str, object] = {
        "message": message or "",
        "project_path": str(project_path),
    }
    events = read_events(project_path)
    last_event = next(
        (event for event in reversed(events) if event.kind != "llm_output_delta"),
        None,
    )
    if last_event is None:
        return diagnostics

    diagnostics["last_event"] = {
        "kind": last_event.kind,
        "atomic_action": last_event.atomic_action,
        "status": last_event.status,
        "routing_decision": last_event.routing_decision,
        "message": redact_sensitive_values(last_event.message, redaction_values),
        "artifact_path": last_event.artifact_path,
    }
    if last_event.artifact_path:
        reasons = _artifact_reasons(project_path / last_event.artifact_path)
        if reasons:
            diagnostics["artifact_reasons"] = [
                redact_sensitive_values(reason, redaction_values)
                for reason in reasons
            ]
    return diagnostics


def _render_failure_diagnostics(diagnostics: dict[str, object]) -> str:
    project_path = diagnostics.get("project_path")
    if not project_path:
        return ""

    lines = ["", f"Inspect project: {project_path}"]
    last_event = diagnostics.get("last_event")
    if isinstance(last_event, dict):
        lines.append(
            "Last harness event: "
            f"{last_event.get('kind')}"
            f" action={last_event.get('atomic_action') or 'none'}"
            f" status={last_event.get('status')}"
            f" route={last_event.get('routing_decision') or 'none'}"
        )
        message = last_event.get("message")
        if isinstance(message, str):
            lines.append("Last message: " + message)
        artifact_path = last_event.get("artifact_path")
        if isinstance(artifact_path, str) and artifact_path:
            lines.append(f"Last artifact: {artifact_path}")
    artifact_reasons = diagnostics.get("artifact_reasons")
    if isinstance(artifact_reasons, list) and artifact_reasons:
        lines.append("Artifact reasons:")
        lines.extend(f"- {reason}" for reason in artifact_reasons if isinstance(reason, str))
    return "\n" + "\n".join(lines)


def _artifact_reasons(path: Path) -> list[str]:
    payload = read_json(path, default={})
    if not isinstance(payload, dict):
        return []
    reasons = payload.get("reasons")
    if isinstance(reasons, list):
        return [str(reason) for reason in reasons[:5]]
    return []


def _restore_runtime_state(previous_project_path: Path | None, previous_profile_id: str | None) -> None:
    _restore_active_profile(previous_profile_id)
    if previous_project_path is not None and previous_project_path.exists():
        project_storage.set_active_project(previous_project_path)
    else:
        project_storage.close_active_project()


def _restore_active_profile(profile_id: str | None) -> None:
    document = profile_storage.load_profiles()
    if profile_id is not None and any(profile.id == profile_id for profile in document.profiles):
        profile_storage.select_profile(profile_id)
        return
    document.active_profile_id = None
    profile_storage.save_profiles(document)


def _default_project_title() -> str:
    return "Novelpilot Live Smoke " + datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def render_text(result: LiveProviderSmokeResult) -> str:
    lines = [
        "Novelpilot live provider smoke passed.",
        f"Project: {result.project_name}",
        f"Path: {result.project_path}",
        f"Profile: {result.profile_id}",
        f"Provider/model: {result.provider_snapshot} / {result.model_snapshot}",
        f"Events: {result.event_count}",
        "",
        "Manual literary review files:",
    ]
    lines.extend(f"- {path}" for path in result.manual_review_paths)
    lines.extend(
        [
            "",
            "Record review with:",
            (
                "npm.cmd run review:literary -- --project "
                f"\"{result.project_path}\" --decision approved "
                "--chapter-assessment \"...\" --state-patch-assessment \"...\""
            ),
        ]
    )
    lines.extend(["", "Artifacts:"])
    lines.extend(f"- {name}: {path}" for name, path in result.artifacts.items())
    return "\n".join(lines) + "\n"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a real LLM provider smoke test through the local Novelpilot harness."
    )
    parser.add_argument("--profile-id", help="LLM profile id to test. Defaults to the active profile.")
    parser.add_argument("--title", help="Novel project title for the generated smoke project.")
    parser.add_argument(
        "--skip-profile-test",
        action="store_true",
        help="Skip the small explicit profile connectivity probe before the full harness run.",
    )
    parser.add_argument(
        "--keep-active",
        action="store_true",
        help="Leave the generated smoke project and profile selected after the command finishes.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    options = LiveProviderSmokeOptions(
        profile_id=args.profile_id,
        title=args.title,
        skip_profile_test=args.skip_profile_test,
        keep_active=args.keep_active,
    )
    try:
        result = run_smoke(options)
    except LiveProviderSmokeError as exc:
        if args.json:
            print(json.dumps({"status": "failed", "message": str(exc)}, ensure_ascii=False, indent=2))
        else:
            print(f"Novelpilot live provider smoke failed: {exc}", file=sys.stderr)
        return exc.exit_code

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(render_text(result), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
