from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.agents.probe import (  # noqa: E402
    ProfileCapabilityProbeError,
    probe_stored_profile,
)
from app.core.config import LLM_PROFILES_PATH  # noqa: E402
from app.profiles import ProfileCatalog  # noqa: E402


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Probe one exact local model Profile through its production Adapter."
    )
    parser.add_argument("profile_id")
    parser.add_argument(
        "--config",
        type=Path,
        default=LLM_PROFILES_PATH,
        help="Local schema-v2 profile document.",
    )
    parser.add_argument(
        "--require-tools",
        action="store_true",
        help="Also require one native function-tool call.",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="Run the probe without updating capability evidence.",
    )
    arguments = parser.parse_args(argv)

    catalog = ProfileCatalog(arguments.config.resolve())
    profile = catalog.get_stored(arguments.profile_id)
    print(
        "Capability probe started: "
        f"profile={profile.id} protocol={profile.api_family} model={profile.model_id} "
        f"tools={str(arguments.require_tools).lower()}",
        flush=True,
    )
    try:
        evidence = asyncio.run(
            probe_stored_profile(
                profile,
                require_tool_calling=arguments.require_tools,
            )
        )
    except ProfileCapabilityProbeError as exc:
        print(f"Capability probe failed: {exc}", file=sys.stderr, flush=True)
        return 1
    if not arguments.no_write:
        catalog.record_capability_evidence(
            profile_id=profile.id,
            evidence=evidence,
        )
    print(
        "Capability probe passed: "
        f"profile={profile.id} protocol={profile.api_family} model={profile.model_id} "
        f"fingerprint={evidence.profile_fingerprint} "
        f"saved={str(not arguments.no_write).lower()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
