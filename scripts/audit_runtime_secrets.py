from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import DATA_DIR, LLM_PROFILES_PATH, OUTPUT_DIR  # noqa: E402
from app.profiles import ProfileCatalog  # noqa: E402
from app.security.audit import SecretAuditResult, audit_runtime_paths  # noqa: E402


def render_text(result: SecretAuditResult) -> str:
    lines = [
        f"NovelPilot runtime secret audit: {result.status}",
        f"Roots: {', '.join(result.roots)}",
        f"Profiles checked: {result.profile_count}",
        f"Files scanned: {result.scanned_file_count}",
    ]
    if result.findings:
        lines.append("Findings:")
        lines.extend(
            f"- {finding.path}: profile={finding.profile_id} kind={finding.kind}"
            for finding in result.findings
        )
    else:
        lines.append("No configured profile API keys were found in databases, backups, exports, or reports.")
    return "\n".join(lines) + "\n"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Scan NovelPilot databases, backups, exports, and reports for configured API keys."
    )
    parser.add_argument(
        "--path",
        action="append",
        type=Path,
        dest="paths",
        help="Override the default data/ and output/ roots; may be repeated.",
    )
    parser.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    roots = arguments.paths or [DATA_DIR, OUTPUT_DIR]
    result = audit_runtime_paths(
        roots=roots,
        profiles=ProfileCatalog(LLM_PROFILES_PATH).load(),
    )
    if arguments.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(render_text(result), end="")
    return 0 if result.status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
