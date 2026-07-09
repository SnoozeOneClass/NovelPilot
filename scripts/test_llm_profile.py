from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from fastapi import HTTPException

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.api import profiles as profiles_api  # noqa: E402
from app.llm.redaction import redact_profile_secrets  # noqa: E402
from app.storage import profiles as profile_storage  # noqa: E402


class ProfileTestCliError(RuntimeError):
    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(frozen=True)
class ProfileTestCliResult:
    profile_id: str
    ok: bool
    model_snapshot: str
    provider_snapshot: str
    message: str

    def to_dict(self) -> dict[str, object]:
        return {
            "profile_id": self.profile_id,
            "ok": self.ok,
            "model_snapshot": self.model_snapshot,
            "provider_snapshot": self.provider_snapshot,
            "message": self.message,
        }


def test_profile(profile_id: str | None) -> ProfileTestCliResult:
    resolved_profile_id = _resolve_profile_id(profile_id)
    try:
        result = profiles_api.test_profile(resolved_profile_id)
    except HTTPException as exc:
        exit_code = 2 if exc.status_code == 404 else 1
        profile = _get_profile_or_none(resolved_profile_id)
        raise ProfileTestCliError(
            redact_profile_secrets(str(exc.detail), profile),
            exit_code=exit_code,
        ) from exc

    profile = _get_profile_or_none(result.profile_id)
    return ProfileTestCliResult(
        profile_id=result.profile_id,
        ok=result.ok,
        model_snapshot=result.model_snapshot,
        provider_snapshot=result.provider_snapshot,
        message=redact_profile_secrets(result.message, profile),
    )


def _resolve_profile_id(requested_profile_id: str | None) -> str:
    if requested_profile_id:
        return requested_profile_id

    document = profile_storage.load_profiles()
    if document.active_profile_id is None:
        raise ProfileTestCliError(
            "No active LLM profile is configured. Use `npm.cmd run profile:upsert -- --select ...` "
            "or pass `--profile-id <id>`.",
            exit_code=2,
        )
    return document.active_profile_id


def _get_profile_or_none(profile_id: str):
    try:
        return profile_storage.get_profile(profile_id)
    except KeyError:
        return None


def render_text(result: ProfileTestCliResult) -> str:
    lines = [
        "LLM profile test passed.",
        f"Profile: {result.profile_id}",
        f"Provider/model: {result.provider_snapshot} / {result.model_snapshot}",
        f"Message: {result.message}",
    ]
    return "\n".join(lines) + "\n"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Test a configured Novelpilot LLM profile.")
    parser.add_argument("--profile-id", help="Profile id to test. Defaults to the active profile.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = test_profile(args.profile_id)
    except ProfileTestCliError as exc:
        if args.json:
            print(json.dumps({"status": "failed", "message": str(exc)}, ensure_ascii=False, indent=2))
        else:
            print(f"LLM profile test failed: {exc}", file=sys.stderr)
        return exc.exit_code

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(render_text(result), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
