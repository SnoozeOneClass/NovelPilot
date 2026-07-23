from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_clean_slate_acceptance_inventory_is_fully_owned_by_offline_evidence() -> None:
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

    assert report["engineering_acceptance_requires_live_success"] is False
    assert report["summary"] == {
        "covered": 17,
        "partial": 0,
        "missing": 0,
        "total": 17,
    }
    assert all(item["status"] == "covered" for item in report["criteria"])
    assert by_id["legacy_runtime_removed"]["status"] == "covered"
    assert by_id["live_observation_ready"]["status"] == "covered"
    assert "post-acceptance observation" in report["scope"]


def test_acceptance_markdown_states_live_result_is_not_the_engineering_gate() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [sys.executable, "scripts/acceptance_report.py"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "# NovelPilot Clean-Slate Acceptance Report" in completed.stdout
    assert "Summary: 17 covered, 0 partial, 0 missing, 17 total." in completed.stdout
    assert "four-run real-model series" in completed.stdout
    assert "legacy_runtime_removed [covered]" in completed.stdout
