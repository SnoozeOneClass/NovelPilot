import json
import subprocess
import sys
from pathlib import Path


def test_acceptance_report_marks_manual_gates_as_manual_required() -> None:
    repo_root = Path(__file__).resolve().parents[2]

    completed = subprocess.run(
        [sys.executable, "scripts/acceptance_report.py", "--json"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(completed.stdout)
    by_id = {item["id"]: item for item in report["criteria"]}
    manual_gate_ids = {
        item["id"] for item in report["criteria"] if item["status"] == "manual_required"
    }

    assert report["scope"].startswith("Static repository evidence map.")
    assert report["summary"] == {
        "covered": 16,
        "partial": 0,
        "manual_required": 2,
        "missing": 0,
        "total": 18,
    }
    assert manual_gate_ids == {"live_provider_smoke", "literary_quality_review"}
    assert by_id["project_lifecycle"]["status"] == "covered"
    assert "stable internal storage identities" in by_id["project_lifecycle"]["requirement"]
    assert by_id["book_setup"]["status"] == "covered"
    assert "formal title as the final discussion decision" in by_id["book_setup"]["requirement"]
    assert by_id["operation_modes"]["status"] == "covered"
    assert "bypassing pending story-arc review gates" in (
        by_id["operation_modes"]["requirement"]
    )
    assert all(
        evidence["ok"]
        for criterion_id in {"project_lifecycle", "book_setup", "operation_modes"}
        for evidence in by_id[criterion_id]["evidence"]
    )
    assert by_id["llm_profiles"]["status"] == "covered"
    assert by_id["live_provider_smoke"]["status"] == "manual_required"
    assert by_id["literary_quality_review"]["status"] == "manual_required"
    assert "API key" in by_id["live_provider_smoke"]["manual_note"]


def test_acceptance_report_markdown_contains_summary() -> None:
    repo_root = Path(__file__).resolve().parents[2]

    completed = subprocess.run(
        [sys.executable, "scripts/acceptance_report.py"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "# Novelpilot Acceptance Report" in completed.stdout
    assert "Static repository evidence map." in completed.stdout
    assert "Summary: 16 covered, 0 partial, 2 manual required, 0 missing, 18 total." in completed.stdout
    assert "stable internal storage identities" in completed.stdout
    assert "formal title as the final discussion decision" in completed.stdout
    assert "bypassing pending story-arc review gates" in completed.stdout
    assert "manual required" in completed.stdout
