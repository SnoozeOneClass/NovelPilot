from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from app.authoring.config import (
    DEFAULT_AUTHORING_MODEL_METADATA_PATH,
    authoring_database_path,
)
from app.authoring.domain.models import RunStatus
from app.authoring.models import AuthoringProfileResolver
from app.authoring.models.catalog import ProfileCatalog
from app.authoring.runtime.engine import ProfileResolver
from app.authoring.service import AuthoringService, fake_profile
from app.authoring.store.store import AuthoringStore
from app.authoring.tools.gateway import ToolGateway
from app.authoring.workers.runtime import (
    PydanticWorkerRuntime,
    ScriptedAutoWorkerRuntime,
    WorkerRuntime,
)
from app.core.config import LLM_PROFILES_PATH


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run NovelPilot's isolated fully automatic authoring engine."
    )
    parser.add_argument("--database", help="SQLite path or sqlite+aiosqlite URL")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--brief", help="Create and automatically finish a new novel")
    action.add_argument("--resume", metavar="PROJECT_ID", help="Resume a paused project")
    action.add_argument("--status", metavar="PROJECT_ID", help="Show project status")
    action.add_argument(
        "--export", metavar="PROJECT_ID", help="Write the stable manuscript snapshot"
    )
    parser.add_argument("--target-chapters", type=int)
    parser.add_argument("--target-words", type=int)
    parser.add_argument("--format", choices=("markdown", "txt"), default="markdown")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-instructions", type=int)
    parser.add_argument("--profiles", type=Path, default=LLM_PROFILES_PATH)
    parser.add_argument(
        "--model-metadata", type=Path, default=DEFAULT_AUTHORING_MODEL_METADATA_PATH
    )
    parser.add_argument(
        "--profile",
        action="append",
        default=[],
        metavar="ROLE=PROFILE_ID",
        help="Bind default/architect/writer/editor/arbiter or fallback:ROLE",
    )
    parser.add_argument(
        "--fake",
        action="store_true",
        help="Use deterministic synthetic prose; does not validate a real model Provider",
    )
    parser.add_argument(
        "--real",
        action="store_true",
        help="Explicitly authorize calls to the configured paid Provider",
    )
    return parser


def _emit_event(kind: str, payload: dict[str, Any]) -> None:
    print(json.dumps({"kind": kind, "payload": payload}, ensure_ascii=False), file=sys.stderr)


async def _main(args: argparse.Namespace) -> int:
    store = AuthoringStore(authoring_database_path(args.database))
    worker: WorkerRuntime | None
    profile_resolver: ProfileResolver
    if args.fake and args.real:
        raise SystemExit("--fake and --real are mutually exclusive")
    if args.profile and not args.real:
        raise SystemExit("--profile bindings are used only with --real")
    if args.profile and not args.brief:
        raise SystemExit("--profile bindings may be set only when creating a project")
    if args.fake:
        worker = ScriptedAutoWorkerRuntime(ToolGateway(store))
        profile_resolver = fake_profile
    elif args.real:
        worker = PydanticWorkerRuntime()
        profile_resolver = AuthoringProfileResolver(
            ProfileCatalog(args.profiles), args.model_metadata
        )
    else:
        worker = None
        profile_resolver = fake_profile
    service = AuthoringService(store, worker_runtime=worker, profile_resolver=profile_resolver)
    await service.initialize()
    project_id: str
    if args.brief:
        if worker is None:
            raise SystemExit("creating a run requires explicit --fake or --real mode")
        bindings = _parse_bindings(args.profile)
        project_id = await service.create(
            brief=args.brief,
            target_chapters=args.target_chapters,
            target_words=args.target_words,
            profile_bindings=bindings,
        )
        _emit_event("project_created", {"project_id": project_id})
        result = await service.run(project_id, max_instructions=args.max_instructions)
        _emit_event("run_finished", result.model_dump(mode="json"))
        if result.status is RunStatus.COMPLETED:
            manuscript, _ = await service.export(project_id, format=args.format)
            print(manuscript, end="")
            return 0
        return 2 if result.status in {RunStatus.PAUSED, RunStatus.FAILURE_PAUSED} else 3
    if args.resume:
        if worker is None:
            raise SystemExit("resuming a run requires explicit --fake or --real mode")
        project_id = args.resume
        await service.resume(project_id, run=False)
        result = await service.run(project_id, max_instructions=args.max_instructions)
        _emit_event("run_finished", result.model_dump(mode="json"))
        return 0 if result.status is RunStatus.COMPLETED else 2
    if args.status:
        view = await service.status(args.status)
        print(view.model_dump_json(indent=2))
        return 0
    if args.export:
        manuscript, digest = await service.export(args.export, format=args.format)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(manuscript, encoding="utf-8")
            print(json.dumps({"output": str(args.output), "sha256": digest}))
        else:
            print(manuscript, end="")
        return 0
    _parser().print_help()
    return 0


def _parse_bindings(values: list[str]) -> dict[str, str]:
    allowed = {
        "default",
        "architect",
        "writer",
        "editor",
        "arbiter",
        "fallback",
        "fallback:architect",
        "fallback:writer",
        "fallback:editor",
        "fallback:arbiter",
    }
    result: dict[str, str] = {}
    for value in values:
        key, separator, profile_id = value.partition("=")
        if not separator or key not in allowed or not profile_id.strip():
            raise SystemExit(f"invalid --profile binding: {value!r}")
        result[key] = profile_id.strip()
    return result


def main() -> int:
    return asyncio.run(_main(_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
