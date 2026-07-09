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

from app.storage.secret_audit import SecretAuditResult, audit_output_for_profile_secrets  # noqa: E402


def render_text(result: SecretAuditResult) -> str:
    lines = [
        f"Novelpilot output secret audit: {result.status}",
        f"Root: {result.root_path}",
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
        lines.append("No configured profile API keys or base URLs were found under output.")
    return "\n".join(lines) + "\n"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan Novelpilot output projects for configured LLM profile secrets."
    )
    parser.add_argument("--output-dir", type=Path, help="Output directory to scan. Defaults to ./output.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    result = audit_output_for_profile_secrets(args.output_dir)
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(render_text(result), end="")
    return 0 if result.status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
