from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from pydantic import ValidationError

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.config import LLM_PROFILES_PATH  # noqa: E402
from app.schemas.profiles import LlmProfilePublic, LlmProfileUpsert  # noqa: E402
from app.storage import profiles as profile_storage  # noqa: E402


@dataclass(frozen=True)
class ProfileConfigureResult:
    profile: LlmProfilePublic
    active_profile_id: str | None
    config_path: str

    def to_dict(self) -> dict[str, object]:
        return {
            "profile": self.profile.model_dump(mode="json"),
            "active_profile_id": self.active_profile_id,
            "config_path": self.config_path,
        }


def configure_profile(
    *,
    profile_id: str,
    name: str,
    protocol: str,
    base_url: str,
    model: str,
    api_key_env: str | None,
    enabled: bool,
    select: bool,
) -> ProfileConfigureResult:
    api_key = _api_key_from_env(api_key_env) if api_key_env is not None else None
    profile = profile_storage.upsert_profile(
        LlmProfileUpsert(
            id=profile_id,
            name=name,
            protocol=protocol,
            base_url=base_url,
            api_key=api_key,
            model=model,
            enabled=enabled,
        )
    )
    if select:
        document = profile_storage.select_profile(profile.id)
    else:
        document = profile_storage.list_public_profiles()
    return ProfileConfigureResult(
        profile=profile,
        active_profile_id=document.active_profile_id,
        config_path=str(LLM_PROFILES_PATH),
    )


def _api_key_from_env(env_name: str) -> str:
    value = os.environ.get(env_name)
    if value is None or not value.strip():
        raise ValueError(f"Environment variable is not set or is blank: {env_name}")
    return value


def render_text(result: ProfileConfigureResult) -> str:
    active = "yes" if result.active_profile_id == result.profile.id else "no"
    lines = [
        "LLM profile saved.",
        f"Profile: {result.profile.id}",
        f"Name: {result.profile.name}",
        f"Protocol/model: {result.profile.protocol} / {result.profile.model}",
        f"Enabled: {result.profile.enabled}",
        f"Has API key: {result.profile.has_api_key}",
        f"Active: {active}",
        f"Config: {result.config_path}",
    ]
    return "\n".join(lines) + "\n"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create or update a local Novelpilot LLM profile. API keys are read "
            "from an environment variable so they do not need to appear in shell history."
        )
    )
    parser.add_argument("--id", required=True, help="Profile id, for example main.")
    parser.add_argument("--name", required=True, help="Human-readable profile name.")
    parser.add_argument(
        "--protocol",
        required=True,
        choices=["openai-compatible", "anthropic-compatible"],
        help="Provider protocol.",
    )
    parser.add_argument("--base-url", required=True, help="Provider base URL.")
    parser.add_argument("--model", required=True, help="Model name to request.")
    parser.add_argument(
        "--api-key-env",
        help=(
            "Environment variable containing the API key. Required for new profiles; "
            "optional for updates that should preserve the existing key."
        ),
    )
    parser.add_argument("--disabled", action="store_true", help="Save the profile as disabled.")
    parser.add_argument("--select", action="store_true", help="Select this profile after saving it.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = configure_profile(
            profile_id=args.id,
            name=args.name,
            protocol=args.protocol,
            base_url=args.base_url,
            model=args.model,
            api_key_env=args.api_key_env,
            enabled=not args.disabled,
            select=args.select,
        )
    except (ValidationError, ValueError) as exc:
        payload = {"status": "failed", "message": str(exc)}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(f"Could not save LLM profile: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(render_text(result), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
