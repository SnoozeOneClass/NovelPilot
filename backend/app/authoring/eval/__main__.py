from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict, cast

from app.authoring.config import DEFAULT_AUTHORING_MODEL_METADATA_PATH
from app.authoring.domain.models import RunStatus, WorkerRole, content_hash
from app.authoring.eval.diagnostics import diagnose
from app.authoring.eval.evidence import (
    FailureArtifacts,
    ReportError,
    retain_failure_evidence,
    safe_failure,
)
from app.authoring.eval.judge import (
    JUDGE_PROMPT,
    DeterministicFakeJudge,
    PydanticLLMJudge,
)
from app.authoring.models import AuthoringProfileResolver
from app.authoring.models.catalog import ProfileCatalog
from app.authoring.service import AuthoringService
from app.authoring.store.store import AuthoringStore
from app.authoring.tools.gateway import ToolGateway
from app.authoring.workers.agents import ROLE_INSTRUCTIONS
from app.authoring.workers.runtime import PydanticWorkerRuntime, ScriptedAutoWorkerRuntime
from app.core.config import LLM_PROFILES_PATH, ROOT_DIR


class EvalCase(TypedDict):
    brief: str
    target_chapters: int


CASES: dict[str, EvalCase] = {
    "smoke": {"brief": "一个守钟人必须在黎明前兑现承诺", "target_chapters": 1},
    "full-auto-3-chapters": {
        "brief": "一个不会写小说的人输入创意后，故事必须自己走到结局",
        "target_chapters": 3,
    },
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate the isolated authoring engine")
    parser.add_argument("--case", choices=tuple(CASES), default="smoke")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fake", action="store_true", help="Use the deterministic no-network Worker")
    mode.add_argument(
        "--real",
        action="store_true",
        help="Explicitly authorize the configured paid Provider for this Eval run",
    )
    parser.add_argument("--report-dir", type=Path)
    parser.add_argument("--profiles", type=Path, default=LLM_PROFILES_PATH)
    parser.add_argument(
        "--model-metadata", type=Path, default=DEFAULT_AUTHORING_MODEL_METADATA_PATH
    )
    parser.add_argument("--profile", help="Profile id bound to every authoring role")
    parser.add_argument("--variant", default="baseline", help="Versioned prompt/config variant id")
    parser.add_argument("--baseline-report", type=Path)
    parser.add_argument("--judge", choices=("none", "fake", "real"), default="none")
    return parser


async def _run(args: argparse.Namespace) -> int:
    if args.fake and args.profile:
        raise SystemExit("--profile is used only with --real Eval runs")
    if args.fake and getattr(args, "judge", "none") == "real":
        raise SystemExit("--judge real requires a --real Eval run")
    report_dir = args.report_dir or Path("data") / "authoring-eval" / args.case
    report_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="novelpilot-authoring-eval-") as temporary:
        store = AuthoringStore(Path(temporary) / "authoring.sqlite3")
        real_resolver: AuthoringProfileResolver | None = None
        if args.fake:
            service = AuthoringService(
                store, worker_runtime=ScriptedAutoWorkerRuntime(ToolGateway(store))
            )
        else:
            real_resolver = AuthoringProfileResolver(
                ProfileCatalog(args.profiles), args.model_metadata
            )
            service = AuthoringService(
                store,
                worker_runtime=PydanticWorkerRuntime(),
                profile_resolver=real_resolver,
            )
        await service.initialize()
        case = CASES[args.case]
        project_id = await service.create(
            brief=case["brief"],
            target_chapters=case["target_chapters"],
            profile_bindings=(
                None
                if args.profile is None
                else {
                    "default": args.profile,
                    "architect": args.profile,
                    "writer": args.profile,
                    "editor": args.profile,
                    "arbiter": args.profile,
                }
            ),
        )
        report: dict[str, Any] = {
            "schema_version": 2,
            "case": args.case,
            "variant": args.variant,
            "fake": bool(args.fake),
            "project_id": project_id,
            "evidence_scope": (
                "deterministic_baseline_only" if args.fake else "real_provider_attempt_failed"
            ),
            "real_provider_validated": False,
            "created_at": datetime.now(UTC).isoformat(),
            "case_fingerprint": content_hash(case),
            "result": None,
            "verdict": "FAIL",
            "deterministic_failures": [],
            "diagnostics": None,
            "judge": None,
            "judge_request": None,
            "manuscript": None,
            "comparison": None,
        }
        manuscript: str | None = None
        stage = "provenance"
        comparison_rejection: SystemExit | None = None
        try:
            prompt_fingerprints = {
                role.value: content_hash(prompt) for role, prompt in ROLE_INSTRUCTIONS.items()
            }
            prompt_fingerprints["judge"] = content_hash(JUDGE_PROMPT)
            report.update(
                source_revision=_source_revision(),
                source_fingerprint=_source_fingerprint(),
                prompt_fingerprints=prompt_fingerprints,
            )
            stage = "run"
            result = await service.run(project_id)
            report["result"] = result.model_dump(mode="json")
            if result.last_error is not None:
                report["result"]["last_error"] = safe_failure(result.last_error)
            stage = "export"
            manuscript, manuscript_hash = await service.export(project_id)
            report.update(manuscript="manuscript.md", manuscript_sha256=manuscript_hash)
            stage = "diagnostics"
            events = await service.events(project_id)
            diagnostic = await diagnose(store, project_id)
            execution_evidence = await store.execution_evidence(project_id)
            chapter_count = (await service.status(project_id)).chapter_count
            deterministic_failures = [
                "run did not complete" if result.status is not RunStatus.COMPLETED else "",
                "chapter count differs from target"
                if chapter_count != case["target_chapters"]
                else "",
                "missing chapter_committed event"
                if not any(event["kind"] == "chapter_committed" for event in events)
                else "",
            ]
            deterministic_failures = [failure for failure in deterministic_failures if failure]
            verdict = (
                "FAIL"
                if deterministic_failures or diagnostic.verdict == "FAIL"
                else ("WARN" if diagnostic.verdict == "WARN" else "PASS")
            )
            successful_requests = [
                row for row in execution_evidence["requests"] if row["status"] == "succeeded"
            ]
            real_provider_validated = bool(
                args.real
                and result.status is RunStatus.COMPLETED
                and successful_requests
                and not deterministic_failures
                and diagnostic.verdict != "FAIL"
            )
            report.update(
                evidence_scope=(
                    "deterministic_baseline_only"
                    if args.fake
                    else (
                        "real_provider_completed_run"
                        if real_provider_validated
                        else "real_provider_attempt_failed"
                    )
                ),
                real_provider_validated=real_provider_validated,
                prompt_fingerprints_exercised=bool(
                    args.real and any(event["kind"] == "model_request_started" for event in events)
                ),
                verdict=verdict,
                deterministic_failures=deterministic_failures,
                event_count=len(events),
                chapter_count=chapter_count,
                diagnostics=diagnostic.model_dump(mode="json"),
                execution_evidence=execution_evidence,
                usage_totals=_usage_totals(execution_evidence),
            )
            stage = "judge"
            judge_result, judge_evidence = await _judge(
                args, real_resolver, store, project_id, manuscript
            )
            report.update(judge=judge_result, judge_request=judge_evidence)
            if judge_evidence is not None:
                totals = report["usage_totals"]
                totals["input_tokens"] += judge_evidence["input_tokens"]
                totals["output_tokens"] += judge_evidence["output_tokens"]
                totals["cache_tokens"] += (
                    judge_evidence["cache_read_tokens"] + judge_evidence["cache_write_tokens"]
                )
                totals["cost_microunits"] += judge_evidence["cost_microunits"]
            stage = "comparison"
            report["comparison"] = _compare(args.baseline_report, report)
        except SystemExit as error:
            # _compare's deliberate rejection must still retain this run's facts.
            comparison_rejection = error
            report["report_error"] = ReportError(stage=stage, error_type=type(error).__name__)
            report["verdict"] = "FAIL"
        except Exception as error:  # noqa: BLE001 - Preserve evidence at the Eval failure boundary.
            # A completed workflow must survive an export, Judge or reporting failure.
            # Never serialize raw exceptions: Provider bodies and validation inputs are unsafe.
            report["report_error"] = ReportError(stage=stage, error_type=type(error).__name__)
            report["verdict"] = "FAIL"
        if report["verdict"] == "FAIL":
            report["failure_evidence"] = await asyncio.to_thread(
                retain_failure_evidence, store.database_path, project_id, report_dir
            )
        try:
            _write_reports(report_dir, report, manuscript)
        except OSError as error:
            # Latest report aliases can be unwritable even when unique artifacts can be saved.
            report["report_write_error"] = ReportError(
                stage="write_report", error_type=type(error).__name__
            )
            report.setdefault("report_error", report["report_write_error"])
            report["verdict"] = "FAIL"
            if "failure_evidence" not in report:
                report["failure_evidence"] = await asyncio.to_thread(
                    retain_failure_evidence, store.database_path, project_id, report_dir
                )
            _write_reports(report_dir, report, manuscript, aliases=False)
    if report["verdict"] == "FAIL":
        # Detailed requests, events and Tool results stay in the referenced artifacts.
        output = {
            key: report.get(key)
            for key in (
                "case",
                "variant",
                "verdict",
                "result",
                "real_provider_validated",
                "evidence_scope",
                "report_error",
                "failure_evidence",
                "usage_totals",
            )
        }
        print(json.dumps(output, ensure_ascii=False))
    else:
        print(json.dumps(report, ensure_ascii=False))
    if comparison_rejection is not None:
        raise comparison_rejection
    return 1 if report["verdict"] == "FAIL" else 0


def _usage_totals(evidence: dict[str, Any]) -> dict[str, int]:
    request_rows = evidence["requests"]
    rows = (
        [row for row in request_rows if row["status"] == "succeeded"]
        if request_rows
        else evidence["usage"]
    )
    return {
        "input_tokens": sum(int(row["input_tokens"]) for row in rows),
        "output_tokens": sum(int(row["output_tokens"]) for row in rows),
        "cache_tokens": sum(
            int(row["cache_read_tokens"]) + int(row["cache_write_tokens"])
            if request_rows
            else int(row["cache_tokens"])
            for row in rows
        ),
        "latency_ms": sum(int(row["latency_ms"]) for row in request_rows or rows),
        "cost_microunits": sum(int(row["cost_microunits"]) for row in request_rows or rows),
    }


async def _judge(
    args: argparse.Namespace,
    resolver: AuthoringProfileResolver | None,
    store: AuthoringStore,
    project_id: str,
    manuscript: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if getattr(args, "judge", "none") == "fake":
        return (await DeterministicFakeJudge().evaluate(manuscript)).model_dump(mode="json"), None
    if getattr(args, "judge", "none") != "real":
        return None, None
    if resolver is None:
        raise RuntimeError("real Judge requires a real Profile resolver")
    selection = await resolver.resolve(store, project_id, WorkerRole.ARBITER)
    try:
        if selection.model is None:
            raise RuntimeError("real Judge Profile did not resolve a Provider model")
        judge = PydanticLLMJudge(selection.model, selection.snapshot)
        result = (await judge.evaluate(manuscript)).model_dump(mode="json")
        return result, judge.request_evidence
    finally:
        await selection.aclose()


def _write_reports(
    report_dir: Path,
    report: dict[str, Any],
    manuscript: str | None,
    *,
    aliases: bool = True,
) -> None:
    artifacts = cast(FailureArtifacts | None, report.get("failure_evidence"))
    if artifacts is not None and manuscript is not None:
        (report_dir / artifacts["manuscript"]).write_text(manuscript, encoding="utf-8")
        report["manuscript"] = artifacts["manuscript"]
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    result = report.get("result") or {}
    totals = report.get("usage_totals") or {}
    markdown = (
        f"# Authoring Eval: {report['case']}\n\n"
        f"- Verdict: **{report['verdict']}**\n"
        f"- Evidence scope: `{report['evidence_scope']}`\n"
        f"- Real Provider validated: **{str(report['real_provider_validated']).lower()}**\n"
        f"- Source revision: `{report.get('source_revision', 'unavailable')}`\n"
        f"- Chapters: {report.get('chapter_count', 'unavailable')}\n"
        f"- Routed instructions: {result.get('instructions_completed', 'unavailable')}\n"
        f"- Events: {report.get('event_count', 'unavailable')}\n"
        f"- Input/output tokens: {totals.get('input_tokens', 'unavailable')}/{totals.get('output_tokens', 'unavailable')}\n"
        f"- Cost microunits: {totals.get('cost_microunits', 'unavailable')}\n"
        f"- Manuscript SHA-256: `{report.get('manuscript_sha256', 'unavailable')}`\n"
    )
    if report.get("report_error"):
        error = report["report_error"]
        markdown += f"- Evaluation error: `{error['stage']}` / `{error['error_type']}`\n"
    if artifacts is not None:
        first = artifacts["first_failure"]
        if first is not None:
            markdown += (
                f"- First recorded failure: `{first['source']}` "
                f"`{first.get('seq', first.get('id'))}` / `{first['kind']}`\n"
            )
        markdown += (
            f"- [Retained SQLite database]({artifacts['database']})\n"
            f"- [Ordered failure and Tool evidence]({artifacts['diagnostics']})\n"
            f"- [Retained report JSON]({artifacts['report_json']})\n"
        )
        (report_dir / artifacts["report_json"]).write_text(encoded, encoding="utf-8")
        (report_dir / artifacts["report_markdown"]).write_text(markdown, encoding="utf-8")
    if aliases:
        if manuscript is not None:
            (report_dir / "manuscript.md").write_text(manuscript, encoding="utf-8")
        (report_dir / "report.json").write_text(encoded, encoding="utf-8")
        (report_dir / "report.md").write_text(markdown, encoding="utf-8")


def _source_revision() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT_DIR,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else "unavailable"


def _source_fingerprint() -> str:
    root = ROOT_DIR / "backend" / "app" / "authoring"
    material = [
        {"path": str(path.relative_to(ROOT_DIR)), "sha256": content_hash(path.read_text("utf-8"))}
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix in {".py", ".sql"}
    ]
    return content_hash(material)


def _compare(path: Path | None, current: dict[str, object]) -> dict[str, object] | None:
    if path is None:
        return None
    baseline = json.loads(path.read_text(encoding="utf-8"))
    if baseline.get("case_fingerprint") != current.get("case_fingerprint"):
        raise SystemExit("A/B reports must use the same Eval case")
    if not baseline.get("prompt_fingerprints_exercised") or not current.get(
        "prompt_fingerprints_exercised"
    ):
        raise SystemExit("A/B reports must come from runs that exercised the compared prompts")
    baseline_prompts = baseline.get("prompt_fingerprints")
    current_prompts = current.get("prompt_fingerprints")
    if not isinstance(baseline_prompts, dict) or not isinstance(current_prompts, dict):
        raise SystemExit("A/B reports lack prompt fingerprints")
    changed_prompts = sorted(
        key
        for key in set(baseline_prompts) | set(current_prompts)
        if baseline_prompts.get(key) != current_prompts.get(key)
    )
    if len(changed_prompts) != 1:
        raise SystemExit("A/B reports must change exactly one prompt variable")
    baseline_profiles = _profile_fingerprints(baseline)
    current_profiles = _profile_fingerprints(current)
    if not baseline_profiles or baseline_profiles != current_profiles:
        raise SystemExit("A/B reports must use identical Profile metadata snapshots")
    baseline_usage = baseline.get("usage_totals", {})
    current_usage = current["usage_totals"]
    if not isinstance(baseline_usage, dict) or not isinstance(current_usage, dict):
        raise SystemExit("baseline report lacks usage_totals")
    return {
        "baseline_report": str(path),
        "baseline_variant": baseline.get("variant"),
        "changed_prompt": changed_prompts[0] if changed_prompts else None,
        "input_token_delta": int(current_usage["input_tokens"])
        - int(baseline_usage.get("input_tokens", 0)),
        "output_token_delta": int(current_usage["output_tokens"])
        - int(baseline_usage.get("output_tokens", 0)),
        "cost_microunit_delta": int(current_usage["cost_microunits"])
        - int(baseline_usage.get("cost_microunits", 0)),
    }


def _profile_fingerprints(report: dict[str, object]) -> set[tuple[str, str, str | None]]:
    evidence = report.get("execution_evidence")
    if not isinstance(evidence, dict):
        return set()
    episodes = evidence.get("episodes")
    if not isinstance(episodes, list):
        return set()
    result: set[tuple[str, str, str | None]] = set()
    for episode in episodes:
        if not isinstance(episode, dict):
            return set()
        profile = episode.get("profile_snapshot")
        fallback = episode.get("fallback_profile_snapshot")
        if not isinstance(profile, dict):
            return set()
        worker = episode.get("worker")
        fingerprint = profile.get("fingerprint")
        fallback_fingerprint = fallback.get("fingerprint") if isinstance(fallback, dict) else None
        if not isinstance(worker, str) or not isinstance(fingerprint, str):
            return set()
        if fallback_fingerprint is not None and not isinstance(fallback_fingerprint, str):
            return set()
        result.add((worker, fingerprint, fallback_fingerprint))
    return result


def main() -> int:
    return asyncio.run(_run(_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
