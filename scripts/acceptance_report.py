from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


Status = Literal["covered", "partial", "manual_required", "missing"]
REPORT_SCOPE = (
    "Static repository evidence map. A covered item means expected files and textual evidence are "
    "present; dynamic behavior is verified by the quality gate commands and any listed manual gates."
)


@dataclass(frozen=True)
class EvidenceProbe:
    path: str
    contains: tuple[str, ...] = ()


@dataclass(frozen=True)
class AcceptanceCriterion:
    id: str
    requirement: str
    probes: tuple[EvidenceProbe, ...]
    manual_note: str | None = None


CRITERIA: tuple[AcceptanceCriterion, ...] = (
    AcceptanceCriterion(
        id="project_lifecycle",
        requirement=(
            "Users can start untitled novels with stable internal storage identities or "
            "continue existing local projects."
        ),
        probes=(
            EvidenceProbe(
                "backend/app/api/projects.py",
                ("create_project", "open_project", "update_operation_mode"),
            ),
            EvidenceProbe(
                "backend/app/storage/projects.py",
                ("project-{metadata.project_id}", "summarize_project"),
            ),
            EvidenceProbe(
                "backend/tests/test_projects.py",
                (
                    "test_multiple_untitled_projects_have_unique_stable_directories",
                    "test_open_project_switches_single_active_project",
                    "test_reopen_project_restores_content_progress_and_mode",
                    "test_project_lifecycle_rejects_active_runner_before_status_transition",
                ),
            ),
            EvidenceProbe(
                "frontend/src/features/project-selector/ProjectSelector.tsx",
                ("新建小说", "openProject", "未命名新书"),
            ),
        ),
    ),
    AcceptanceCriterion(
        id="llm_profiles",
        requirement="Users can configure multiple local LLM profiles for OpenAI and Anthropic protocols.",
        probes=(
            EvidenceProbe("backend/app/api/profiles.py", ("test_profile", "select_profile")),
            EvidenceProbe("backend/app/llm/openai_compatible.py"),
            EvidenceProbe("backend/app/llm/anthropic_compatible.py"),
            EvidenceProbe("frontend/src/features/llm-profiles/LlmProfilesPanel.tsx", ("testProfile",)),
        ),
    ),
    AcceptanceCriterion(
        id="secret_safety",
        requirement="LLM secrets are stored only in gitignored local config, not novel output.",
        probes=(
            EvidenceProbe(".gitignore", ("config/*.local.json", "output/")),
            EvidenceProbe("backend/tests/test_profiles.py", ("masks_api_key", "preserves_existing_api_key")),
            EvidenceProbe("backend/tests/test_happy_path.py", ("secret-key", "_project_tree_contains")),
            EvidenceProbe("backend/app/storage/secret_audit.py", ("audit_output_for_profile_secrets",)),
            EvidenceProbe("package.json", ("audit:secrets",)),
        ),
    ),
    AcceptanceCriterion(
        id="book_setup",
        requirement=(
            "Book direction uses open-ended co-creation, confirms the formal title as the final "
            "discussion decision, and atomically approves that title with the reviewed direction."
        ),
        probes=(
            EvidenceProbe(
                "backend/app/api/setup.py",
                ("continue_setup_discussion", "prepare_setup_review", "approve_setup"),
            ),
            EvidenceProbe(
                "backend/app/harness/loops/book.py",
                (
                    "assemble_discussion_context",
                    "confirmed_decision_coverage",
                    "recommended_titles",
                    "review_book_direction",
                ),
            ),
            EvidenceProbe(
                "backend/app/storage/setup.py",
                (
                    "candidate_revision",
                    "title_suggestions_path",
                    'files["project.json"]',
                    "SetupRevisionConflict",
                ),
            ),
            EvidenceProbe(
                "backend/tests/test_setup.py",
                (
                    "test_explicit_approval_requires_latest_revision",
                    "test_explicit_approval_rejects_title_not_confirmed_in_discussion",
                    "test_book_discussion_tool_requires_confirmed_title_before_review_ready",
                    "test_approval_transaction_rolls_back_partial_formal_artifacts",
                    "test_review_blocks_candidate_without_confirmed_decision_coverage",
                    "test_setup_api_failure_is_fail_closed",
                ),
            ),
            EvidenceProbe(
                "backend/tests/test_readiness.py",
                ("test_readiness_fails_closed_when_approved_setup_has_no_title",),
            ),
            EvidenceProbe(
                "frontend/src/features/setup-conversation/SetupConversation.tsx",
                (
                    "prepareSetupReview",
                    "approveSetup(candidate.revision, finalTitle)",
                ),
            ),
            EvidenceProbe(
                "frontend/src/features/setup-conversation/SetupReview.tsx",
                (
                    "selectedTitle",
                    "正式书名已经在逐问讨论的最后一步由你确认",
                    "批准并采用",
                ),
            ),
            EvidenceProbe(
                "frontend/src/features/setup-conversation/SetupConversation.test.tsx",
                (
                    "uses the standard one-question flow for the final formal-title decision",
                    "renders the discussion-confirmed title without another title picker",
                ),
            ),
        ),
    ),
    AcceptanceCriterion(
        id="book_setup_durability",
        requirement=(
            "Book discussion and approval resist stale concurrent results, partial writes, "
            "and duplicate event replay."
        ),
        probes=(
            EvidenceProbe(
                "backend/app/storage/transactions.py",
                ("commit_file_transaction", "recover_file_transactions"),
            ),
            EvidenceProbe(
                "backend/app/storage/events.py",
                ("exclusive_file_lock", "existing.event_id == event.event_id"),
            ),
            EvidenceProbe(
                "backend/tests/test_transactions.py",
                ("rolls_back_all_targets", "recovers_after_process_stops_mid_commit"),
            ),
            EvidenceProbe(
                "backend/tests/test_setup.py",
                (
                    "test_stale_discussion_result_cannot_overwrite_newer_revision",
                    "test_approval_transaction_rolls_back_partial_formal_artifacts",
                    "test_setup_api_queues_events_when_durable_append_temporarily_fails",
                ),
            ),
        ),
    ),
    AcceptanceCriterion(
        id="operation_modes",
        requirement=(
            "Users can safely change a novel's operation mode without racing active runs or "
            "bypassing pending story-arc review gates or Book revision approval gates."
        ),
        probes=(
            EvidenceProbe(
                "backend/app/api/projects.py",
                ("update_operation_mode", "begin_active_runner"),
            ),
            EvidenceProbe(
                "backend/app/storage/projects.py",
                ("operation_mode_changed", "project_metadata_lock", ".event-outbox"),
            ),
            EvidenceProbe(
                "backend/tests/test_projects.py",
                (
                    "test_mode_change_marks_existing_unapproved_arc_for_review",
                    "test_mode_change_to_full_auto_preserves_pending_arc_gate",
                    "test_mode_change_rejects_run_lock",
                    "test_mode_change_rejects_active_runner_before_status_transition",
                    "test_participatory_to_full_auto_fails_closed_when_active_arc_state_is_missing",
                    "test_profile_sync_and_mode_change_preserve_each_others_metadata",
                ),
            ),
            EvidenceProbe(
                "backend/tests/test_orchestrator.py",
                (
                    "test_participatory_arc_waits_for_approval",
                    "test_pending_arc_review_is_not_bypassed_after_switch_to_full_auto",
                ),
            ),
            EvidenceProbe(
                "backend/app/harness/orchestrator.py",
                (
                    "_current_arc_requires_human_review",
                    'human_review == "awaiting_review"',
                    "book_revision_approval_required",
                    "including in full-auto mode",
                ),
            ),
            EvidenceProbe(
                "backend/app/storage/book_revisions.py",
                ("awaiting_approval", "expected_base_book_version"),
            ),
            EvidenceProbe(
                "backend/tests/test_book_revisions.py",
                ("test_full_auto_book_revision_stays_candidate_until_explicit_approval",),
            ),
            EvidenceProbe(
                "frontend/src/features/settings/SettingsView.tsx",
                ("changeMode", "api.updateProjectMode", "modeLocked"),
            ),
        ),
    ),
    AcceptanceCriterion(
        id="feedback_checkpoint",
        requirement="User feedback is recorded immediately and processed at safe checkpoints.",
        probes=(
            EvidenceProbe("backend/app/api/feedback.py", ("user_feedback",)),
            EvidenceProbe("backend/app/harness/orchestrator.py", ("_process_pending_feedback", "_feedback_prompt_block")),
            EvidenceProbe("backend/tests/test_orchestrator.py", ("test_orchestrator_injects_feedback_after_context_snapshot_exists",)),
            EvidenceProbe("frontend/src/features/workspace/Workspace.tsx", ("submitFeedback",)),
        ),
    ),
    AcceptanceCriterion(
        id="rolling_arc",
        requirement="Story arc planning is rolling/current-arc-only, not a full upfront roadmap.",
        probes=(
            EvidenceProbe("backend/app/harness/orchestrator.py", ("_plan_initial_story_arc", "Do not plan the full book")),
            EvidenceProbe("backend/tests/test_orchestrator.py", ("test_completed_arc_rolls_to_next_arc_plan",)),
            EvidenceProbe("README.md", ("滚动规划当前故事弧",)),
        ),
    ),
    AcceptanceCriterion(
        id="candidate_committed_boundaries",
        requirement="Chapter artifacts distinguish candidate material from committed canon.",
        probes=(
            EvidenceProbe("backend/app/schemas/artifacts.py", ("CandidateObservations", "ChapterVerification")),
            EvidenceProbe("backend/app/storage/artifacts.py", ("candidate_observations", "committed_state_patch")),
            EvidenceProbe("backend/tests/test_patches.py", ("observations", "canon")),
        ),
    ),
    AcceptanceCriterion(
        id="state_patch_commit",
        requirement="LLM-generated candidate state patches are harness-validated before canon commit.",
        probes=(
            EvidenceProbe("backend/app/storage/patches.py", ("validate_candidate_state_patch", "commit_candidate_state_patch")),
            EvidenceProbe("backend/tests/test_patches.py", ("test_patch", "evidence")),
            EvidenceProbe(
                "backend/app/harness/agents/domain_tools.py",
                ("submit_chapter_candidate", "SubmitChapterCandidateInput"),
            ),
            EvidenceProbe(
                "backend/app/harness/orchestrator.py",
                ("candidate_state_patch.json", "commit_state_patch"),
            ),
        ),
    ),
    AcceptanceCriterion(
        id="domain_canon",
        requirement="Canonical state is split into domain-specific files.",
        probes=(
            EvidenceProbe("backend/app/storage/projects.py", ("canon/characters.json", "canon/relationships.json", "canon/world_facts.json", "canon/foreshadowing.json")),
            EvidenceProbe("README.md", ("characters.json", "foreshadowing.json")),
        ),
    ),
    AcceptanceCriterion(
        id="workspace_ui",
        requirement="Frontend provides one state-derived creation flow with live prose and secondary diagnostics.",
        probes=(
            EvidenceProbe(
                "frontend/src/features/creation/CreationView.tsx",
                ("CreationView", "LiveChapterDocument", "StoryArcReviewTask", "CreationDetailsSheet"),
            ),
            EvidenceProbe(
                "frontend/src/features/evidence/EvidenceCenter.tsx",
                ("事件时间线", "运行轨迹", "验证", "useVirtualizer"),
            ),
            EvidenceProbe(
                "frontend/src/features/creation/CreationView.module.css",
                (".creation", ".readingColumn", ".composer"),
            ),
        ),
    ),
    AcceptanceCriterion(
        id="run_control_sse",
        requirement="Harness has one explicit start, backend-owned durable continuation, abnormal recovery, and replayable streaming updates.",
        probes=(
            EvidenceProbe("backend/app/api/runs.py", ("start_run", "pause_run", "resume_run", "stream_events")),
            EvidenceProbe("backend/tests/test_runs.py", ("_events_after_last_event_id", "concurrent")),
            EvidenceProbe("backend/app/harness/run_host.py", ("class RunHost", "route_run", "begin_harness_checkpoint")),
            EvidenceProbe("backend/app/harness/flow_router.py", ("RunFacts", "def route_run")),
            EvidenceProbe("frontend/src/features/workspace/Workspace.tsx", ("useHarnessEvents", "startRun", "waiting_for_provider")),
            EvidenceProbe("frontend/src/app/harness-events.ts", ("EventSource", "chapter_draft_delta")),
        ),
    ),
    AcceptanceCriterion(
        id="retry_recovery",
        requirement="Failed verification or patch rejection can be retried without deleting audit evidence.",
        probes=(
            EvidenceProbe("backend/app/api/runs.py", ("retry_current_chapter", "retry_manifest.json")),
            EvidenceProbe("backend/tests/test_runs.py", ("test_retry_current_chapter",)),
            EvidenceProbe("frontend/src/features/workspace/Workspace.tsx", ("retryCurrentChapter",)),
        ),
    ),
    AcceptanceCriterion(
        id="export",
        requirement="Export generates a manuscript from committed chapter finals only.",
        probes=(
            EvidenceProbe("backend/app/api/exports.py", ("export_manuscript",)),
            EvidenceProbe("backend/app/storage/export.py", ("final.md", "manuscript.md")),
            EvidenceProbe("backend/tests/test_export.py", ("draft", "final")),
        ),
    ),
    AcceptanceCriterion(
        id="docs",
        requirement="Public architecture notes, local usage, README, and validation commands are documented.",
        probes=(
            EvidenceProbe("docs/architecture.md", ("三层 Loop", "候选状态与已提交状态")),
            EvidenceProbe("docs/local-usage.md", ("质量门禁", "真实 Provider Smoke")),
            EvidenceProbe("README.md", ("验证", "存储模型")),
            EvidenceProbe("package.json", ("typecheck", "lint", "test")),
        ),
    ),
    AcceptanceCriterion(
        id="live_provider_smoke",
        requirement="Full local flow works against a real configured LLM provider.",
        probes=(
            EvidenceProbe("backend/app/api/profiles.py", ("test_profile",)),
            EvidenceProbe("scripts/live_provider_smoke.py", ("run_smoke", "live_smoke_report.json")),
            EvidenceProbe("package.json", ("smoke:live",)),
            EvidenceProbe("README.md", ("smoke test",)),
        ),
        manual_note=(
            "Requires a user-supplied API key/profile; run "
            "`npm.cmd run smoke:live -- --profile-id <id>` before marking complete."
        ),
    ),
    AcceptanceCriterion(
        id="literary_quality_review",
        requirement="A real generated chapter and state patch are inspected for usefulness.",
        probes=(
            EvidenceProbe("backend/tests/test_happy_path.py", ("test_local_happy_path",)),
            EvidenceProbe(
                "backend/app/storage/completion.py",
                ("record_literary_review", "literary_review.json"),
            ),
            EvidenceProbe(
                "backend/app/api/completion.py",
                ("create_literary_review", "get_completion_audit"),
            ),
            EvidenceProbe("scripts/completion_audit.py", ("literary_quality_review", "build_completion_audit")),
            EvidenceProbe("package.json", ("review:literary", "audit:completion")),
        ),
        manual_note=(
            "Fixture tests prove mechanics; literary usefulness requires reviewing a real provider "
            "run and recording `exports/literary_review.json`."
        ),
    ),
)


def build_report(repo_root: Path) -> dict[str, object]:
    items = [evaluate_criterion(repo_root, criterion) for criterion in CRITERIA]
    summary = {
        "covered": sum(1 for item in items if item["status"] == "covered"),
        "partial": sum(1 for item in items if item["status"] == "partial"),
        "manual_required": sum(1 for item in items if item["status"] == "manual_required"),
        "missing": sum(1 for item in items if item["status"] == "missing"),
        "total": len(items),
    }
    return {"scope": REPORT_SCOPE, "summary": summary, "criteria": items}


def evaluate_criterion(repo_root: Path, criterion: AcceptanceCriterion) -> dict[str, object]:
    evidence = [evaluate_probe(repo_root, probe) for probe in criterion.probes]
    passed = sum(1 for item in evidence if item["ok"])
    if criterion.manual_note is not None:
        status: Status = "manual_required" if passed == len(evidence) else "partial"
    elif passed == len(evidence):
        status = "covered"
    elif passed == 0:
        status = "missing"
    else:
        status = "partial"

    return {
        "id": criterion.id,
        "requirement": criterion.requirement,
        "status": status,
        "manual_note": criterion.manual_note,
        "evidence": evidence,
    }


def evaluate_probe(repo_root: Path, probe: EvidenceProbe) -> dict[str, object]:
    path = repo_root / probe.path
    if not path.exists():
        return {"path": probe.path, "ok": False, "reason": "missing file"}
    if not probe.contains:
        return {"path": probe.path, "ok": True, "reason": "file exists"}

    text = path.read_text(encoding="utf-8", errors="replace")
    missing = [needle for needle in probe.contains if needle not in text]
    if missing:
        return {
            "path": probe.path,
            "ok": False,
            "reason": "missing text: " + ", ".join(missing),
        }
    return {"path": probe.path, "ok": True, "reason": "file contains expected text"}


def render_markdown(report: dict[str, object]) -> str:
    summary = report["summary"]
    assert isinstance(summary, dict)
    lines = [
        "# Novelpilot Acceptance Report",
        "",
        str(report["scope"]),
        "",
        (
            f"Summary: {summary['covered']} covered, {summary['partial']} partial, "
            f"{summary['manual_required']} manual required, {summary['missing']} missing, "
            f"{summary['total']} total."
        ),
        "",
    ]
    criteria = report["criteria"]
    assert isinstance(criteria, list)
    for item in criteria:
        assert isinstance(item, dict)
        lines.extend(
            [
                f"## {item['id']} [{item['status']}]",
                "",
                str(item["requirement"]),
                "",
            ]
        )
        manual_note = item.get("manual_note")
        if manual_note:
            lines.extend([f"Manual note: {manual_note}", ""])
        evidence = item["evidence"]
        assert isinstance(evidence, list)
        for evidence_item in evidence:
            assert isinstance(evidence_item, dict)
            mark = "OK" if evidence_item["ok"] else "MISS"
            lines.append(f"- {mark}: `{evidence_item['path']}` - {evidence_item['reason']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a static Novelpilot acceptance report.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of Markdown.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    report = build_report(repo_root)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(report), end="")


if __name__ == "__main__":
    main()
