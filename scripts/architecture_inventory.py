from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast


Status = Literal["covered", "partial", "missing"]
REPORT_SCOPE = (
    "Non-verdict static architecture inventory for the clean-slate SQLite/Pydantic-AI "
    "implementation. 'Covered' means only that named source and local contract ownership "
    "exist. It never proves that the backend production path works; that verdict requires "
    "the paid 5.4-mini real-scenario command."
)


@dataclass(frozen=True, slots=True)
class EvidenceProbe:
    path: str
    contains: tuple[str, ...] = ()
    excludes: tuple[str, ...] = ()
    must_not_exist: bool = False


@dataclass(frozen=True, slots=True)
class AcceptanceCriterion:
    id: str
    requirement: str
    probes: tuple[EvidenceProbe, ...]


CRITERIA: tuple[AcceptanceCriterion, ...] = (
    AcceptanceCriterion(
        id="authoritative_sqlite_store",
        requirement="One async SQLite database and its Alembic schema own all runtime state.",
        probes=(
            EvidenceProbe("backend/app/db/schema.py", ("EXPECTED_TABLE_NAMES", "agent_task_attempts")),
            EvidenceProbe("backend/app/db/engine.py", ("create_sqlite_async_engine", "journal_mode=WAL")),
            EvidenceProbe("backend/tests/db/test_schema.py", ("test_initial_revision_supports_empty_database_lifecycle",)),
            EvidenceProbe("backend/tests/test_database_engine.py", ("test_app_lifespan_owns_clean_runtime_resources",)),
        ),
    ),
    AcceptanceCriterion(
        id="project_owned_content",
        requirement="Large content is project-owned CAS data with no cross-project Blob lifecycle.",
        probes=(
            EvidenceProbe("backend/app/store/content.py", ("canonical-json-v1", "exact-utf8-v1", "redacted-bytes-v1")),
            EvidenceProbe("backend/tests/store/test_content.py", ("test_project_scoped_dedup",)),
            EvidenceProbe("backend/tests/db/test_constraints.py", ("content", "project")),
        ),
    ),
    AcceptanceCriterion(
        id="lt1_lifecycle",
        requirement="Book, Story Arc, and Chapter have explicit workspaces, reviews, and formal baselines.",
        probes=(
            EvidenceProbe("backend/app/domain/book/commands.py", ("BookCommandService", "approve_and_commit")),
            EvidenceProbe("backend/app/domain/arc/commands.py", ("ArcCommandService", "approve_and_commit")),
            EvidenceProbe("backend/app/domain/chapter/commands.py", ("ChapterCommandService", "commit_chapter_and_canon")),
            EvidenceProbe("backend/tests/domain/test_revisions.py", ("baseline", "revision")),
        ),
    ),
    AcceptanceCriterion(
        id="book_gate",
        requirement="Every mode uses open Book co-creation, independent evaluation, and explicit user approval.",
        probes=(
            EvidenceProbe("backend/app/domain/book/discussion.py", ("bind_agent_result", "bind_user_input")),
            EvidenceProbe("backend/app/api/workspace.py", ("record_book_input", "approve_book")),
            EvidenceProbe("backend/tests/domain/test_book_discussion.py", ("suggestion", "title")),
            EvidenceProbe("backend/tests/runtime/test_domain_driver.py", ("assert book_gates == 1",)),
        ),
    ),
    AcceptanceCriterion(
        id="arc_mode_gate",
        requirement="Full-auto has no Arc approval, while participatory has exactly one persistent approval per Arc.",
        probes=(
            EvidenceProbe("backend/app/domain/arc/commands.py", ("arc.approval_required", "policy_auto", "human_approval")),
            EvidenceProbe("backend/tests/runtime/test_domain_driver.py", ("arc_gates == (0 if operation_mode == \"full_auto\" else 2)",)),
            EvidenceProbe("backend/tests/domain/test_arc_lifecycle.py", ("approval_gate", "full_auto")),
        ),
    ),
    AcceptanceCriterion(
        id="chapter_canon_atomicity",
        requirement="Chapter plan/draft/observe/evaluate commits prose and optional Canon in one command boundary.",
        probes=(
            EvidenceProbe("backend/app/runtime/driver.py", ("chapter.plan", "chapter.draft", "chapter.observe", "evaluate.chapter")),
            EvidenceProbe("backend/app/domain/chapter/canon.py", ("CanonPatch",)),
            EvidenceProbe("backend/tests/domain/test_chapter_lifecycle.py", ("canon", "baseline")),
        ),
    ),
    AcceptanceCriterion(
        id="pydantic_ai_core",
        requirement="Pydantic AI owns Provider execution; model IDs are opaque and capabilities fail closed.",
        probes=(
            EvidenceProbe("backend/app/agents/binding.py", ("ModelBindingResolver", "api_family")),
            EvidenceProbe("backend/app/agents/contracts.py", ("native_json_schema", "text_streaming", "ProfileSnapshot")),
            EvidenceProbe("backend/tests/test_pydantic_ai_contract.py", ("test_plain_text_stream", "test_native_output")),
            EvidenceProbe("backend/tests/agents/test_binding.py", ("test_model_id_is_opaque", "zero_provider_requests")),
        ),
    ),
    AcceptanceCriterion(
        id="bounded_provider_policy",
        requirement="Each activation has at most six requests, five transport retries, and fixed T1 timeouts.",
        probes=(
            EvidenceProbe("backend/app/agents/transport.py", ("PROVIDER_REQUEST_LIMIT", "TRANSPORT_RETRY_LIMIT")),
            EvidenceProbe("backend/app/agents/contracts.py", ("provider-timeout-t1-v1", "ACTIVATION_TIMEOUT_MS")),
            EvidenceProbe("backend/tests/agents/test_transport.py", ("six", "retry")),
            EvidenceProbe("backend/app/db/schema.py", ("provider_request_count <= 6", "transport_retry_count <= 5")),
        ),
    ),
    AcceptanceCriterion(
        id="single_run_engine_recovery",
        requirement="FastAPI lifespan owns one async Run Engine with pause, dedicated retry, and C1 replay.",
        probes=(
            EvidenceProbe("backend/app/runtime/resources.py", ("RunEngine", "async def start", "async def close")),
            EvidenceProbe("backend/app/runtime/reconcile.py", ("crash_replay", "failure_paused")),
            EvidenceProbe("backend/tests/runtime/test_engine.py", ("test_global_slot_prevents_two_engines",)),
            EvidenceProbe("backend/tests/runtime/test_reconcile.py", ("crash_replay", "failure_paused")),
        ),
    ),
    AcceptanceCriterion(
        id="evidence_live_separation",
        requirement="Task evidence is durable while token deltas remain lossy in-memory signals.",
        probes=(
            EvidenceProbe("backend/app/agents/executor.py", ("EvidenceItemDraft", "diagnostic_attachment")),
            EvidenceProbe("backend/app/runtime/live.py", ("LossyLiveFanout",)),
            EvidenceProbe("backend/tests/agents/test_executor.py", ("without_token_deltas", "list_attempt_summaries")),
            EvidenceProbe("backend/tests/runtime/test_live_and_routing.py", ("live", "routing")),
        ),
    ),
    AcceptanceCriterion(
        id="feedback_and_revision",
        requirement=(
            "Feedback is queued and applied at safe boundaries; approved-content changes "
            "escalate explicitly and rebase stale workspaces."
        ),
        probes=(
            EvidenceProbe("backend/app/domain/feedback.py", ("QueueFeedbackRequest", "apply")),
            EvidenceProbe("backend/app/domain/change_requests.py", ("ChangeRequest", "activate")),
            EvidenceProbe("backend/tests/domain/test_feedback.py", ("guidance", "context")),
            EvidenceProbe("backend/tests/domain/test_stale_rebase.py", ("rebase", "stale")),
        ),
    ),
    AcceptanceCriterion(
        id="hierarchical_loop_authority",
        requirement=(
            "Book owns an approved ordered Arc topology, Arc owns its complete Chapter "
            "outline, and bounded parent reviews prevent lower layers from replacing "
            "upper baselines."
        ),
        probes=(
            EvidenceProbe(
                "backend/app/domain/authority.py",
                (
                    "record_arc_parent_review",
                    "record_book_parent_review",
                    "_bounded_review_disposition",
                ),
            ),
            EvidenceProbe(
                "backend/app/domain/change_requests.py",
                (
                    "Arc revision lacks current Arc-layer authorization",
                    "Book revision lacks current Book-layer authorization",
                ),
            ),
            EvidenceProbe(
                "backend/tests/domain/test_change_requests.py",
                ("activate-without-authority", "record_arc_revision_authorization"),
            ),
            EvidenceProbe(
                "backend/app/runtime/driver.py",
                ("automatic_correction_round=1", "source_arc_parent_review_id"),
            ),
        ),
    ),
    AcceptanceCriterion(
        id="completion_snapshot_export",
        requirement=(
            "Non-final Arc closure produces a deterministic topology handoff, final Arc "
            "closure precedes Book completion, snapshots are immutable identities, and "
            "Markdown uses committed Chapters only."
        ),
        probes=(
            EvidenceProbe(
                "backend/app/domain/authority.py",
                ("record_arc_closure_review", "commit_book_completion", "book.completed"),
            ),
            EvidenceProbe(
                "backend/tests/domain/test_completion.py",
                ("_prepare_formal_arc_closure", "evaluate.book_completion"),
            ),
            EvidenceProbe("backend/app/domain/snapshots.py", ("ProjectSnapshotManifest", "fingerprint")),
            EvidenceProbe("backend/app/domain/export.py", ("ManuscriptExportResult", "content_sha256")),
            EvidenceProbe("backend/tests/domain/test_export_and_snapshot.py", ("snapshot", "export")),
        ),
    ),
    AcceptanceCriterion(
        id="explicit_api_frontend",
        requirement="The UI consumes authoritative state and explicit commands; reads and SSE do not drive execution.",
        probes=(
            EvidenceProbe("backend/app/api/workspace.py", ("get_project_diagnostics", "stream_events", "Idempotency-Key")),
            EvidenceProbe("backend/tests/api/test_workspace_api.py", ("second_read.json() == first_read.json()",)),
            EvidenceProbe("frontend/src/App.tsx", ("state.commands", "EventSource", "failure_paused")),
            EvidenceProbe("frontend/src/api/workspace-client.ts", ("runControl", "sendBookInput", "approveArc")),
        ),
    ),
    AcceptanceCriterion(
        id="legacy_runtime_removed",
        requirement="No second file-store, hand-written Provider gateway, thread RunHost, or legacy UI client remains.",
        probes=(
            EvidenceProbe("backend/app/harness", must_not_exist=True),
            EvidenceProbe("backend/app/llm", must_not_exist=True),
            EvidenceProbe("backend/app/storage", must_not_exist=True),
            EvidenceProbe("backend/app/schemas", must_not_exist=True),
            EvidenceProbe("frontend/src/api/client.ts", must_not_exist=True),
            EvidenceProbe("backend/app/main.py", excludes=("legacy_runtime_enabled", "RunHost")),
        ),
    ),
    AcceptanceCriterion(
        id="backup_restore_and_secret_audit",
        requirement="Consistent whole-database backup/restore and non-disclosing runtime secret scans are executable.",
        probes=(
            EvidenceProbe("backend/app/db/maintenance.py", ("create_consistent_backup", "restore_database", "validate_backup")),
            EvidenceProbe("backend/tests/db/test_maintenance.py", ("backup", "restore")),
            EvidenceProbe("backend/app/security/audit.py", ("audit_runtime_paths", "api_key")),
            EvidenceProbe("backend/tests/test_secret_audit.py", ("database_backup_export_and_report",)),
        ),
    ),
    AcceptanceCriterion(
        id="live_observation_ready",
        requirement=(
            "A command-owned, non-gating four-slot real-model experiment is frozen, "
            "public-API-only, records zero rescue, and leaves a later-analysis handoff."
        ),
        probes=(
            EvidenceProbe(
                "scripts/live_acceptance_cases/benchmark_mother_natural_book_v1.json",
                ("full_auto", "participatory", "recommended-first-public-api-v1"),
            ),
            EvidenceProbe(
                "scripts/live_book_observation_series.py",
                (
                    "technical_rescue_count",
                    "engineering_acceptance_dependency",
                    "LATEST_SERIES_FILENAME",
                    "heartbeat_seconds",
                    "active_observation",
                    "run_series",
                ),
                ("/run/retry", "/run/resume", "/run/pause"),
            ),
            EvidenceProbe(
                "backend/tests/test_live_observation_series.py",
                (
                    "exact_mode_schedule_without_rescue",
                    "unchanged_long_running_state_emits_truthful_heartbeat",
                    "unexpected_runner_crash_leaves_durable_running_marker",
                ),
            ),
            EvidenceProbe("package.json", ("experiment:live-book",)),
        ),
    ),
    AcceptanceCriterion(
        id="documentation",
        requirement="Architecture, local operation, acceptance traceability, and resume-project framing match the new system.",
        probes=(
            EvidenceProbe("README.md", ("Pydantic AI", "SQLite", "简历")),
            EvidenceProbe("docs/architecture.md", ("Domain Harness", "Transactional Outbox", "LT1")),
            EvidenceProbe("docs/local-usage.md", ("backend:migrate", "experiment:live-book")),
            EvidenceProbe(
                "docs/acceptance-traceability.md",
                ("三层证据", "工程真实场景验收", "用户四次长跑"),
            ),
        ),
    ),
)


def evaluate_probe(repo_root: Path, probe: EvidenceProbe) -> dict[str, object]:
    path = repo_root / probe.path
    if probe.must_not_exist:
        source_files = (
            []
            if not path.is_dir()
            else [
                item
                for item in path.rglob("*")
                if item.is_file() and "__pycache__" not in item.parts
            ]
        )
        absent = not path.exists() or (path.is_dir() and not source_files)
        return {
            "path": probe.path,
            "ok": absent,
            "reason": "path absent" if absent else "forbidden path still contains source files",
        }
    if not path.is_file():
        return {"path": probe.path, "ok": False, "reason": "missing file"}
    text = path.read_text(encoding="utf-8", errors="replace")
    missing = [needle for needle in probe.contains if needle not in text]
    forbidden = [needle for needle in probe.excludes if needle in text]
    if missing or forbidden:
        reasons: list[str] = []
        if missing:
            reasons.append("missing text: " + ", ".join(missing))
        if forbidden:
            reasons.append("forbidden text: " + ", ".join(forbidden))
        return {"path": probe.path, "ok": False, "reason": "; ".join(reasons)}
    return {"path": probe.path, "ok": True, "reason": "contract evidence present"}


def evaluate_criterion(repo_root: Path, criterion: AcceptanceCriterion) -> dict[str, object]:
    evidence = [evaluate_probe(repo_root, probe) for probe in criterion.probes]
    passed = sum(1 for item in evidence if item["ok"])
    status: Status = (
        "covered" if passed == len(evidence) else "missing" if passed == 0 else "partial"
    )
    return {
        "id": criterion.id,
        "requirement": criterion.requirement,
        "status": status,
        "evidence": evidence,
    }


def build_report(repo_root: Path) -> dict[str, object]:
    items = [evaluate_criterion(repo_root, criterion) for criterion in CRITERIA]
    summary = {
        "covered": sum(1 for item in items if item["status"] == "covered"),
        "partial": sum(1 for item in items if item["status"] == "partial"),
        "missing": sum(1 for item in items if item["status"] == "missing"),
        "total": len(items),
    }
    return {
        "scope": REPORT_SCOPE,
        "is_acceptance_verdict": False,
        "project_acceptance_command": "npm.cmd run acceptance",
        "real_scenario_command": "npm.cmd run test:backend-real",
        "summary": summary,
        "criteria": items,
    }


def render_markdown(report: dict[str, object]) -> str:
    summary = report["summary"]
    assert isinstance(summary, dict)
    lines = [
        "# NovelPilot Architecture Ownership Inventory",
        "",
        str(report["scope"]),
        "",
        (
            f"Summary: {summary['covered']} covered, {summary['partial']} partial, "
            f"{summary['missing']} missing, {summary['total']} total."
        ),
        "",
    ]
    criteria = report["criteria"]
    assert isinstance(criteria, list)
    for item in criteria:
        assert isinstance(item, dict)
        lines.extend([f"## {item['id']} [{item['status']}]", "", str(item["requirement"]), ""])
        evidence = item["evidence"]
        assert isinstance(evidence, list)
        for probe in evidence:
            assert isinstance(probe, dict)
            lines.append(
                f"- {'OK' if probe['ok'] else 'MISS'}: `{probe['path']}` - {probe['reason']}"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a non-verdict clean-slate architecture ownership inventory."
    )
    parser.add_argument("--json", action="store_true")
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    report = build_report(Path(__file__).resolve().parents[1])
    if arguments.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_markdown(report), end="")
    summary = cast(dict[str, int], report["summary"])
    return 0 if summary["partial"] == 0 and summary["missing"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
