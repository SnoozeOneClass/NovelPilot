from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = ROOT_DIR / "backend"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.schemas.completion import LiteraryReviewRequest  # noqa: E402
from app.storage.completion import record_literary_review as record_project_literary_review  # noqa: E402
from completion_audit import find_latest_smoke_report  # noqa: E402


def record_literary_review(
    project_path: Path,
    decision: str,
    reviewer: str,
    chapter_assessment: str,
    state_patch_assessment: str,
    notes: str,
) -> dict[str, object]:
    record = record_project_literary_review(
        project_path,
        LiteraryReviewRequest(
            decision=decision,
            reviewer=reviewer,
            chapter_assessment=chapter_assessment,
            state_patch_assessment=state_patch_assessment,
            notes=notes,
        ),
    )
    return record.model_dump(mode="json")


def _resolve_project_path(project: Path | None, smoke_report: Path | None) -> Path:
    if project is not None:
        return project
    if smoke_report is not None:
        return smoke_report.parents[1]
    latest = find_latest_smoke_report(ROOT_DIR)
    if latest is None:
        raise FileNotFoundError(
            "No live smoke report found. Run `npm.cmd run smoke:live -- --profile-id <id>` first."
        )
    return latest.parents[1]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record literary/usefulness review for a smoke run.")
    parser.add_argument("--project", type=Path, help="Smoke project path under output/.")
    parser.add_argument("--smoke-report", type=Path, help="Path to exports/live_smoke_report.json.")
    parser.add_argument("--decision", choices=["approved", "rejected"], required=True)
    parser.add_argument("--reviewer", default="manual reviewer")
    parser.add_argument("--chapter-assessment", required=True)
    parser.add_argument("--state-patch-assessment", required=True)
    parser.add_argument("--notes", default="")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        project_path = _resolve_project_path(args.project, args.smoke_report)
        payload = record_literary_review(
            project_path=project_path,
            decision=args.decision,
            reviewer=args.reviewer,
            chapter_assessment=args.chapter_assessment,
            state_patch_assessment=args.state_patch_assessment,
            notes=args.notes,
        )
    except (FileNotFoundError, ValueError) as exc:
        if args.json:
            print(json.dumps({"status": "failed", "message": str(exc)}, ensure_ascii=False, indent=2))
        else:
            print(f"Could not record literary review: {exc}")
        return 1

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"Literary review recorded: {payload['literary_review_json']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
