from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from app.authoring.domain.models import Instruction, InstructionKind, WorkerRole, content_hash
from app.authoring.eval import __main__ as eval_module
from app.authoring.eval.evidence import retain_failure_evidence
from app.authoring.models.catalog import ProfilesDocument
from app.authoring.models.profiles import EpisodeProfileSelection
from app.authoring.models.transport import (
    ModelRequestBudgetExhausted,
    TransportRetryBudgetExhausted,
)
from app.authoring.security import audit_runtime_paths
from app.authoring.service import AuthoringService, fake_profile
from app.authoring.store.store import AuthoringStore
from app.authoring.tools.gateway import EpisodeDeps, ToolGateway
from app.authoring.workers.runtime import EpisodeResult, ScriptedAutoWorkerRuntime
from pydantic_ai import models
from pydantic_ai.models.function import FunctionModel

PROFILE_SECRET = "eval-provider-secret-DoNotPersist"
UNSAFE_ERROR_INPUT = "private-provider-input-DoNotPersist"


def _args(report_dir: Path) -> argparse.Namespace:
    return argparse.Namespace(
        case="smoke",
        fake=False,
        real=True,
        report_dir=report_dir,
        profiles=report_dir.parent / "unused-profiles.json",
        model_metadata=report_dir.parent / "unused-metadata.json",
        profile=None,
        variant="failure-evidence-test",
        baseline_report=None,
        judge="none",
    )


class ConfiguredTestResolver:
    def __init__(self, *args: object) -> None:
        pass

    async def resolve(self, store: object, project_id: str, role: WorkerRole):
        return EpisodeProfileSelection(
            snapshot=fake_profile(project_id, role),
            redaction_secrets=(PROFILE_SECRET,),
        )


class InterruptedWriter:
    async def run(
        self,
        instruction: Instruction,
        deps: EpisodeDeps,
        cancel_event: asyncio.Event | None = None,
    ) -> EpisodeResult:
        gateway = ToolGateway(deps.store)
        if instruction.kind is not InstructionKind.WRITE_CHAPTER:
            return await ScriptedAutoWorkerRuntime(gateway).run(instruction, deps, cancel_event)
        # Production Service -> Engine -> Gateway -> SQLite. Only the Worker/Provider is fake.
        prior = await deps.store.has_checkpoint(
            deps.project_id, instruction.instruction_key, "plan_chapter", "chapter:1"
        )
        await gateway.invoke(
            deps,
            "plan_chapter",
            {"chapter_number": 1, "plan": "changed retry" if prior else "durable first plan"},
        )
        raise ConnectionError(f"first request failed: {PROFILE_SECRET}; body={UNSAFE_ERROR_INPUT}")


class CompletedWorker:
    async def run(
        self,
        instruction: Instruction,
        deps: EpisodeDeps,
        cancel_event: asyncio.Event | None = None,
    ) -> EpisodeResult:
        index = deps.next_model_request()
        await deps.store.record_runtime_event(
            deps.project_id, "model_request_started", {"episode_id": deps.episode_id}
        )
        await deps.store.record_model_request(
            project_id=deps.project_id,
            episode_id=deps.episode_id,
            request_index=index,
            profile_fingerprint=deps.profile.fingerprint,
            purpose="worker",
            status="succeeded",
            input_tokens=10,
            output_tokens=5,
        )
        return await ScriptedAutoWorkerRuntime(ToolGateway(deps.store)).run(
            instruction, deps, cancel_event
        )


def test_failed_eval_retains_first_failure_and_authoritative_tool_facts(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(eval_module, "AuthoringProfileResolver", ConfiguredTestResolver)
    monkeypatch.setattr(eval_module, "PydanticWorkerRuntime", InterruptedWriter)
    args = _args(tmp_path / "failed")
    with models.override_allow_model_requests(False):
        assert asyncio.run(eval_module._run(args)) == 1
    report = json.loads((args.report_dir / "report.json").read_text("utf-8"))
    assert report["verdict"] == "FAIL"
    assert report["real_provider_validated"] is False
    assert report["result"]["instructions_completed"] == 1
    failed_episodes = report["result"]["episodes_failed"]
    assert failed_episodes >= 2
    assert "ToolConflictError" in report["result"]["last_error"]
    artifacts = report["failure_evidence"]
    diagnostic = json.loads((args.report_dir / artifacts["diagnostics"]).read_text("utf-8"))
    first = diagnostic["first_failure"]
    assert first["kind"] == "episode_failed"
    assert first["payload"]["failure"].startswith("ConnectionError:")
    assert len(diagnostic["episode_failures"]) == failed_episodes
    assert diagnostic["episode_failures"][-1]["failure"].startswith("ToolConflictError:")
    assert [event["seq"] for event in diagnostic["events"]] == sorted(
        event["seq"] for event in diagnostic["events"]
    )
    assert any(event["kind"] == "tool_failed" for event in diagnostic["events"])
    checkpoint = next(row for row in diagnostic["checkpoints"] if row["step"] == "plan_chapter")
    invocation = next(
        row for row in diagnostic["tool_invocations"] if row["tool_name"] == "plan_chapter"
    )
    assert checkpoint["payload_hash"] == content_hash(
        {"chapter_number": 1, "plan": "durable first plan"}
    )
    assert checkpoint["payload_hash"] == invocation["payload_hash"]
    assert checkpoint["result"] == invocation["result"]
    assert checkpoint["result_hash"] == content_hash(checkpoint["result"])
    database = args.report_dir / artifacts["database"]
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT count(*) FROM chapters").fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM planning_revisions WHERE audited=1"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT content FROM content_blobs JOIN chapter_versions ON "
            "content_blobs.sha256=chapter_versions.content_sha256 WHERE kind='plan'"
        ).fetchone() == ("durable first plan",)
        assert connection.execute("SELECT status FROM run_state").fetchone() == ("failure_paused",)
    assert artifacts["database"] in (args.report_dir / "report.md").read_text("utf-8")
    assert (args.report_dir / artifacts["report_json"]).is_file()
    assert (args.report_dir / artifacts["report_markdown"]).is_file()
    profiles = ProfilesDocument.model_validate(
        {
            "profiles": [
                {
                    "id": "test",
                    "display_name": "Test",
                    "api_family": "openai_responses",
                    "model_id": "test",
                    "base_url": "https://test.invalid/v1",
                    "api_key": PROFILE_SECRET,
                }
            ]
        }
    )
    audit = audit_runtime_paths(roots=[args.report_dir], profiles=profiles)
    assert audit.status == "passed", audit.to_dict()
    for path in args.report_dir.rglob("*"):
        if path.is_file():
            assert UNSAFE_ERROR_INPUT.encode() not in path.read_bytes()
    output = capsys.readouterr()
    assert PROFILE_SECRET not in output.out + output.err
    assert UNSAFE_ERROR_INPUT not in output.out + output.err
    assert "durable first plan" not in output.out


def test_failed_eval_repeated_report_directory_preserves_earlier_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(eval_module, "AuthoringProfileResolver", ConfiguredTestResolver)
    monkeypatch.setattr(eval_module, "PydanticWorkerRuntime", InterruptedWriter)
    args = _args(tmp_path / "repeated")
    with models.override_allow_model_requests(False):
        assert asyncio.run(eval_module._run(args)) == 1
        first = json.loads((args.report_dir / "report.json").read_text("utf-8"))
        preserved = {
            key: (args.report_dir / first["failure_evidence"][key]).read_bytes()
            for key in ("database", "diagnostics", "report_json", "report_markdown")
        }
        assert asyncio.run(eval_module._run(args)) == 1
    second = json.loads((args.report_dir / "report.json").read_text("utf-8"))
    assert first["result"]["project_id"] != second["result"]["project_id"]
    for key, value in preserved.items():
        assert first["failure_evidence"][key] != second["failure_evidence"][key]
        assert (args.report_dir / first["failure_evidence"][key]).read_bytes() == value


def test_judge_provider_failure_retains_completed_workflow_without_claiming_eval_passed(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    def provider_failure(messages, info):
        raise ConnectionError(f"{PROFILE_SECRET}: {UNSAFE_ERROR_INPUT}")

    class JudgeFailureResolver(ConfiguredTestResolver):
        async def resolve(self, store, project_id, role):
            return EpisodeProfileSelection(
                snapshot=fake_profile(project_id, role), model=FunctionModel(provider_failure)
            )

    monkeypatch.setattr(eval_module, "AuthoringProfileResolver", JudgeFailureResolver)
    monkeypatch.setattr(eval_module, "PydanticWorkerRuntime", CompletedWorker)
    args = _args(tmp_path / "judge-failed")
    args.judge = "real"
    with models.override_allow_model_requests(False):
        assert asyncio.run(eval_module._run(args)) == 1
    report = json.loads((args.report_dir / "report.json").read_text("utf-8"))
    assert report["result"]["status"] == "completed"
    assert report["diagnostics"]["verdict"] == "PASS"
    assert report["real_provider_validated"] is True
    assert report["deterministic_failures"] == []
    assert report["verdict"] == "FAIL"
    assert report["report_error"] == {"stage": "judge", "error_type": "ConnectionError"}
    assert report["judge"] is None
    assert report["execution_evidence"]["requests"]
    assert report["usage_totals"]["input_tokens"] > 0
    assert (
        content_hash((args.report_dir / report["manuscript"]).read_text("utf-8"))
        == report["manuscript_sha256"]
    )
    with sqlite3.connect(args.report_dir / report["failure_evidence"]["database"]) as connection:
        assert connection.execute("SELECT count(*) FROM chapters").fetchone() == (1,)
        assert connection.execute("SELECT status FROM run_state").fetchone() == ("completed",)
    for path in args.report_dir.iterdir():
        assert PROFILE_SECRET.encode() not in path.read_bytes()
        assert UNSAFE_ERROR_INPUT.encode() not in path.read_bytes()
    output = capsys.readouterr()
    assert PROFILE_SECRET not in output.out + output.err
    assert UNSAFE_ERROR_INPUT not in output.out + output.err


def test_exception_escaping_service_run_still_retains_durable_tool_writes(
    tmp_path: Path, monkeypatch
) -> None:
    class FailingCleanup(EpisodeProfileSelection):
        async def aclose(self) -> None:
            raise RuntimeError(f"unsafe provider cleanup {UNSAFE_ERROR_INPUT}")

    class CleanupFailureResolver(ConfiguredTestResolver):
        async def resolve(self, store, project_id, role):
            return FailingCleanup(snapshot=fake_profile(project_id, role))

    monkeypatch.setattr(eval_module, "AuthoringProfileResolver", CleanupFailureResolver)
    monkeypatch.setattr(eval_module, "PydanticWorkerRuntime", CompletedWorker)
    args = _args(tmp_path / "service-failed")
    with models.override_allow_model_requests(False):
        assert asyncio.run(eval_module._run(args)) == 1
    report = json.loads((args.report_dir / "report.json").read_text("utf-8"))
    assert report["verdict"] == "FAIL"
    assert report["report_error"] == {"stage": "run", "error_type": "RuntimeError"}
    assert report["real_provider_validated"] is False
    assert report["result"] is None  # No invented EngineResult for a run that raised.
    with sqlite3.connect(args.report_dir / report["failure_evidence"]["database"]) as connection:
        assert connection.execute(
            "SELECT count(*) FROM checkpoints WHERE step='audit_foundation'"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM planning_revisions WHERE audited=1"
        ).fetchone() == (1,)
    for path in args.report_dir.iterdir():
        assert UNSAFE_ERROR_INPUT.encode() not in path.read_bytes()


def test_report_alias_failure_preserves_unique_report_and_database(tmp_path: Path) -> None:
    args = _args(tmp_path / "unwritable-alias")
    args.fake, args.real = True, False
    (args.report_dir / "report.json").mkdir(parents=True)
    assert asyncio.run(eval_module._run(args)) == 1
    saved_reports = list(args.report_dir.glob("failed-*.report.json"))
    assert len(saved_reports) == 1
    report = json.loads(saved_reports[0].read_text("utf-8"))
    assert report["verdict"] == "FAIL"
    assert report["result"]["status"] == "completed"
    assert report["report_error"]["stage"] == "write_report"
    assert (args.report_dir / report["failure_evidence"]["database"]).is_file()


def test_ab_rejection_keeps_nonzero_semantics_and_completed_run_evidence(tmp_path: Path) -> None:
    args = _args(tmp_path / "comparison-failed")
    args.fake, args.real = True, False
    args.baseline_report = tmp_path / "baseline.json"
    args.baseline_report.write_text(
        json.dumps({"case_fingerprint": content_hash(eval_module.CASES[args.case])}),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="exercised"):
        asyncio.run(eval_module._run(args))
    report = json.loads((args.report_dir / "report.json").read_text("utf-8"))
    assert report["verdict"] == "FAIL"
    assert report["result"]["status"] == "completed"
    assert report["comparison"] is None
    assert report["report_error"] == {"stage": "comparison", "error_type": "SystemExit"}
    assert (args.report_dir / report["failure_evidence"]["database"]).is_file()


def test_snapshot_includes_committed_wal_and_excludes_uncommitted_writes(tmp_path: Path) -> None:
    database = tmp_path / "source.sqlite3"
    service = AuthoringService(AuthoringStore(database))
    asyncio.run(service.initialize())
    project_id = asyncio.run(service.create("consistent diagnostic snapshot", target_chapters=1))
    with sqlite3.connect(database) as live:
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("UPDATE projects SET title='committed title'")
        live.commit()
        live.execute("UPDATE projects SET title='uncommitted title'")
        artifacts = retain_failure_evidence(database, project_id, tmp_path)
        with sqlite3.connect(tmp_path / artifacts["database"]) as snapshot:
            assert snapshot.execute("SELECT title FROM projects").fetchone() == ("committed title",)
            assert snapshot.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        live.rollback()


@pytest.mark.parametrize(
    "failure",
    [
        ModelRequestBudgetExhausted(
            "Task allows at most 24 semantic model request(s) per frozen Agent run."
        ),
        TransportRetryBudgetExhausted("Task activation exhausted its 2 transport retries."),
    ],
)
def test_first_failed_request_precedes_episode_failure_and_preserves_budget_details(
    tmp_path: Path, monkeypatch, failure
) -> None:
    class BudgetLimitedWriter(CompletedWorker):
        async def run(self, instruction, deps, cancel_event=None):
            if instruction.kind is not InstructionKind.WRITE_CHAPTER:
                return await super().run(instruction, deps, cancel_event)
            await deps.store.record_model_request(
                project_id=deps.project_id,
                episode_id=deps.episode_id,
                request_index=deps.next_model_request(),
                profile_fingerprint=deps.profile.fingerprint,
                purpose="worker",
                status="failed",
                error_type=type(failure).__name__,
            )
            raise failure

    monkeypatch.setattr(eval_module, "AuthoringProfileResolver", ConfiguredTestResolver)
    monkeypatch.setattr(eval_module, "PydanticWorkerRuntime", BudgetLimitedWriter)
    args = _args(tmp_path / "budget-failed")
    with models.override_allow_model_requests(False):
        assert asyncio.run(eval_module._run(args)) == 1
    report = json.loads((args.report_dir / "report.json").read_text("utf-8"))
    diagnostic = json.loads(
        (args.report_dir / report["failure_evidence"]["diagnostics"]).read_text("utf-8")
    )
    first = diagnostic["first_failure"]
    assert first["source"] == "model_requests"
    assert first["payload"]["error_type"] == type(failure).__name__
    assert first["created_at"] < next(
        event["created_at"] for event in diagnostic["events"] if event["kind"] == "episode_failed"
    )
    expected = f"{type(failure).__name__}: {failure}"
    assert report["result"]["last_error"] == expected
    assert diagnostic["episode_failures"][0]["failure"] == expected


def test_tool_validation_input_is_not_copied_into_failure_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    class InvalidInputWorker(CompletedWorker):
        async def run(self, instruction, deps, cancel_event=None):
            if instruction.kind is not InstructionKind.WRITE_CHAPTER:
                return await super().run(instruction, deps, cancel_event)
            await ToolGateway(deps.store).invoke(
                deps, "plan_chapter", {"chapter_number": UNSAFE_ERROR_INPUT, "plan": "safe"}
            )
            raise AssertionError("invalid Tool input was accepted")

    monkeypatch.setattr(eval_module, "AuthoringProfileResolver", ConfiguredTestResolver)
    monkeypatch.setattr(eval_module, "PydanticWorkerRuntime", InvalidInputWorker)
    args = _args(tmp_path / "validation-failed")
    with models.override_allow_model_requests(False):
        assert asyncio.run(eval_module._run(args)) == 1
    report = json.loads((args.report_dir / "report.json").read_text("utf-8"))
    first = report["failure_evidence"]["first_failure"]
    assert first["kind"] == "tool_failed"
    assert first["payload"]["tool"] == "plan_chapter"
    assert first["payload"]["error_type"] == "ValidationError"
    for path in args.report_dir.iterdir():
        assert UNSAFE_ERROR_INPUT.encode() not in path.read_bytes()
