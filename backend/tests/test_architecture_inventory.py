from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_architecture_inventory_is_explicitly_not_an_acceptance_verdict() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [sys.executable, "scripts/architecture_inventory.py", "--json"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(completed.stdout)
    by_id = {item["id"]: item for item in report["criteria"]}

    assert report["is_acceptance_verdict"] is False
    assert report["project_acceptance_command"] == "npm.cmd run acceptance"
    assert report["real_scenario_command"] == "npm.cmd run test:backend-real"
    assert report["summary"] == {
        "covered": 18,
        "partial": 0,
        "missing": 0,
        "total": 18,
    }
    assert all(item["status"] == "covered" for item in report["criteria"])
    assert by_id["hierarchical_loop_authority"]["status"] == "covered"
    assert by_id["legacy_runtime_removed"]["status"] == "covered"
    assert by_id["live_observation_ready"]["status"] == "covered"
    assert "never proves that the backend production path works" in report["scope"]


def test_inventory_markdown_points_to_real_scenario_gate() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [sys.executable, "scripts/architecture_inventory.py"],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "# NovelPilot Architecture Ownership Inventory" in completed.stdout
    assert "Summary: 18 covered, 0 partial, 0 missing, 18 total." in completed.stdout
    assert "paid 5.4-mini real-scenario command" in completed.stdout
    assert "legacy_runtime_removed [covered]" in completed.stdout
