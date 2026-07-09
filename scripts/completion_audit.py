from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence


ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = ROOT_DIR / "backend"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.schemas.completion import CompletionGate as ProjectCompletionGate  # noqa: E402
from app.storage.completion import audit_project_completion, build_output_secret_audit_gate  # noqa: E402
from acceptance_report import build_report  # noqa: E402

GateStatus = Literal["passed", "pending", "failed"]


@dataclass(frozen=True)
class CompletionGate:
    id: str
    status: GateStatus
    message: str
    evidence: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "status": self.status,
            "message": self.message,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class CompletionAudit:
    status: GateStatus
    gates: list[CompletionGate]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "gates": [gate.to_dict() for gate in self.gates],
        }


def build_completion_audit(
    repo_root: Path,
    project_path: Path | None = None,
    smoke_report_path: Path | None = None,
) -> CompletionAudit:
    smoke_report = _resolve_smoke_report(repo_root, project_path, smoke_report_path)
    gates = [
        _static_acceptance_gate(repo_root),
        *_project_gates(repo_root, project_path, smoke_report),
    ]
    status = _overall_status(gates)
    return CompletionAudit(status=status, gates=gates)


def _static_acceptance_gate(repo_root: Path) -> CompletionGate:
    report = build_report(repo_root)
    summary_value = report["summary"]
    assert isinstance(summary_value, dict)
    partial = int(summary_value["partial"])
    missing = int(summary_value["missing"])
    manual_required = int(summary_value["manual_required"])
    if partial or missing:
        return CompletionGate(
            id="static_acceptance",
            status="failed",
            message=f"Static acceptance has {partial} partial and {missing} missing items.",
            evidence=["scripts/acceptance_report.py"],
        )
    if manual_required != 2:
        return CompletionGate(
            id="static_acceptance",
            status="failed",
            message=f"Expected 2 manual gates, found {manual_required}.",
            evidence=["scripts/acceptance_report.py"],
        )
    return CompletionGate(
        id="static_acceptance",
        status="passed",
        message="Static repository acceptance has no partial or missing items.",
        evidence=["scripts/acceptance_report.py"],
    )


def _project_gates(
    repo_root: Path,
    project_path: Path | None,
    smoke_report_path: Path | None,
) -> list[CompletionGate]:
    if smoke_report_path is None or not smoke_report_path.exists():
        return [
            _from_project_gate(build_output_secret_audit_gate(project_path or repo_root / "output")),
            CompletionGate(
                id="live_provider_smoke",
                status="pending",
                message=(
                    "No live smoke report found. Run "
                    "`npm.cmd run smoke:live -- --profile-id <profile-id>`."
                ),
                evidence=[],
            ),
            CompletionGate(
                id="literary_quality_review",
                status="pending",
                message="Literary review waits for a completed live smoke project.",
                evidence=[],
            ),
        ]
    smoke_project_path = smoke_report_path.parents[1]
    audit = audit_project_completion(smoke_project_path)
    return [_from_project_gate(gate) for gate in audit.gates]


def _from_project_gate(gate: ProjectCompletionGate) -> CompletionGate:
    return CompletionGate(
        id=gate.id,
        status=gate.status,
        message=gate.message,
        evidence=gate.evidence,
    )


def _resolve_smoke_report(
    repo_root: Path,
    project_path: Path | None,
    smoke_report_path: Path | None,
) -> Path | None:
    if smoke_report_path is not None:
        return smoke_report_path
    if project_path is not None:
        return project_path / "exports" / "live_smoke_report.json"
    return find_latest_smoke_report(repo_root)


def find_latest_smoke_report(repo_root: Path) -> Path | None:
    output_path = repo_root / "output"
    if not output_path.exists():
        return None
    reports = list(output_path.glob("*/exports/live_smoke_report.json"))
    if not reports:
        return None
    return max(reports, key=lambda path: path.stat().st_mtime)


def _overall_status(gates: Sequence[CompletionGate]) -> GateStatus:
    if any(gate.status == "failed" for gate in gates):
        return "failed"
    if any(gate.status == "pending" for gate in gates):
        return "pending"
    return "passed"


def render_text(audit: CompletionAudit) -> str:
    lines = [f"Novelpilot completion audit: {audit.status}", ""]
    for gate in audit.gates:
        lines.extend([f"- {gate.id}: {gate.status}", f"  {gate.message}"])
        if gate.evidence:
            lines.append("  Evidence:")
            lines.extend(f"  - {item}" for item in gate.evidence)
    return "\n".join(lines) + "\n"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit Novelpilot completion gates.")
    parser.add_argument("--project", type=Path, help="Smoke project path under output/.")
    parser.add_argument("--smoke-report", type=Path, help="Path to exports/live_smoke_report.json.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    audit = build_completion_audit(
        ROOT_DIR,
        project_path=args.project,
        smoke_report_path=args.smoke_report,
    )
    if args.json:
        print(json.dumps(audit.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(render_text(audit), end="")
    return 0 if audit.status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
