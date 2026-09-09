from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import pytest
from app.authoring.domain.models import AuthoringProfileSnapshot
from app.authoring.eval.__main__ import _compare, _run
from app.authoring.eval.judge import PydanticLLMJudge
from pydantic_ai import models
from pydantic_ai.models.test import TestModel


def test_fake_eval_report_contains_reproducible_evidence_and_diagnostics(tmp_path: Path) -> None:
    report_dir = tmp_path / "report"
    args = argparse.Namespace(
        case="smoke",
        fake=True,
        real=False,
        report_dir=report_dir,
        profiles=tmp_path / "unused-profiles.json",
        model_metadata=tmp_path / "unused-metadata.json",
        profile=None,
        variant="prompt-baseline-v1",
        baseline_report=None,
        judge="fake",
    )

    assert asyncio.run(_run(args)) == 0
    report = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))

    assert report["schema_version"] == 2
    assert "failure_evidence" not in report
    assert report["manuscript"] == "manuscript.md"
    assert (report_dir / "manuscript.md").is_file()
    assert (report_dir / "report.md").is_file()
    assert report["source_revision"]
    assert set(report["prompt_fingerprints"]) == {
        "architect",
        "writer",
        "editor",
        "arbiter",
        "judge",
    }
    assert report["diagnostics"]["verdict"] in {"PASS", "WARN"}
    assert report["prompt_fingerprints_exercised"] is False
    assert report["real_provider_validated"] is False
    assert report["judge"]["judge_id"] == "deterministic-fake-judge"
    assert set(report["judge"]["dimensions"]) == {
        "causality",
        "character",
        "pacing",
        "continuity",
        "stakes",
        "prose",
        "payoff",
    }
    assert report["execution_evidence"]["episodes"]
    assert report["usage_totals"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_tokens": 0,
        "latency_ms": 0,
        "cost_microunits": 0,
    }


def test_failed_real_eval_does_not_claim_provider_validation(tmp_path: Path) -> None:
    report_dir = tmp_path / "failed-real"
    args = argparse.Namespace(
        case="smoke",
        fake=False,
        real=True,
        report_dir=report_dir,
        profiles=tmp_path / "missing-profiles.json",
        model_metadata=tmp_path / "missing-metadata.json",
        profile=None,
        variant="real-attempt",
        baseline_report=None,
        judge="none",
    )

    assert asyncio.run(_run(args)) == 1
    report = json.loads((report_dir / "report.json").read_text(encoding="utf-8"))
    assert report["verdict"] == "FAIL"
    assert report["real_provider_validated"] is False
    assert report["evidence_scope"] == "real_provider_attempt_failed"
    assert report["prompt_fingerprints_exercised"] is False


def test_ab_comparison_requires_same_case_and_exactly_one_prompt_change(tmp_path: Path) -> None:
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(
        json.dumps(
            {
                "case_fingerprint": "case-a",
                "variant": "baseline",
                "prompt_fingerprints_exercised": True,
                "prompt_fingerprints": {"writer": "old", "editor": "same"},
                "execution_evidence": {
                    "episodes": [
                        {
                            "worker": "writer",
                            "profile_snapshot": {"fingerprint": "profile-a"},
                            "fallback_profile_snapshot": None,
                        }
                    ]
                },
                "usage_totals": {
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "cost_microunits": 30,
                },
            }
        ),
        encoding="utf-8",
    )
    current: dict[str, object] = {
        "case_fingerprint": "case-a",
        "prompt_fingerprints_exercised": True,
        "prompt_fingerprints": {"writer": "new", "editor": "same"},
        "execution_evidence": {
            "episodes": [
                {
                    "worker": "writer",
                    "profile_snapshot": {"fingerprint": "profile-a"},
                    "fallback_profile_snapshot": None,
                }
            ]
        },
        "usage_totals": {
            "input_tokens": 15,
            "output_tokens": 18,
            "cost_microunits": 35,
        },
    }
    comparison = _compare(baseline_path, current)
    assert comparison is not None
    assert comparison["changed_prompt"] == "writer"
    assert comparison["input_token_delta"] == 5

    current["prompt_fingerprints"] = {"writer": "old", "editor": "same"}
    with pytest.raises(SystemExit, match="exactly one prompt"):
        _compare(baseline_path, current)

    current["prompt_fingerprints"] = {"writer": "new", "editor": "same"}
    current["prompt_fingerprints_exercised"] = False
    with pytest.raises(SystemExit, match="exercised"):
        _compare(baseline_path, current)


def test_versioned_pydantic_judge_uses_seven_dimension_structured_output() -> None:
    profile = AuthoringProfileSnapshot(
        profile_id="judge-profile",
        provider_protocol="test",
        model_id="judge-model",
        context_window=8_192,
        max_output_tokens=512,
        input_price_per_million=1,
        output_price_per_million=2,
        cache_price_per_million=0.5,
    )
    model = TestModel(
        custom_output_args={
            "score": 4,
            "dimensions": {
                "causality": 4,
                "character": 4,
                "pacing": 3,
                "continuity": 5,
                "stakes": 4,
                "prose": 4,
                "payoff": 3,
            },
            "rationale": "The causal chain is clear.",
            "evidence": ["The promise changes the ending."],
        }
    )
    judge = PydanticLLMJudge(model, profile)

    with models.override_allow_model_requests(False):
        result = asyncio.run(judge.evaluate("# Novel\n\n## Chapter 1\n\nA promise."))

    assert result.judge_id == "authoring-seven-dimension-judge"
    assert result.version == 1
    assert result.dimensions.continuity == 5
    assert judge.request_evidence is not None
    assert judge.request_evidence["profile_fingerprint"] == profile.fingerprint
