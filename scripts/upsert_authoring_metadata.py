from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.authoring.config import DEFAULT_AUTHORING_MODEL_METADATA_PATH
from app.authoring.models import upsert_authoring_model_metadata
from app.authoring.models.catalog import ProfileCatalog, ProfileConfigurationError
from app.core.config import LLM_PROFILES_PATH


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Atomically upsert secret-free authoring metadata for one local Profile."
    )
    parser.add_argument("profile_id")
    parser.add_argument("--context-window", type=int, required=True)
    parser.add_argument("--max-output-tokens", type=int, required=True)
    parser.add_argument("--input-price", type=float, default=0.0, metavar="PER_MILLION")
    parser.add_argument("--output-price", type=float, default=0.0, metavar="PER_MILLION")
    parser.add_argument("--cache-price", type=float, default=0.0, metavar="PER_MILLION")
    parser.add_argument("--profiles", type=Path, default=LLM_PROFILES_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_AUTHORING_MODEL_METADATA_PATH)
    arguments = parser.parse_args(argv)
    try:
        metadata = upsert_authoring_model_metadata(
            ProfileCatalog(arguments.profiles.resolve()),
            arguments.output.resolve(),
            profile_id=arguments.profile_id,
            context_window=arguments.context_window,
            max_output_tokens=arguments.max_output_tokens,
            input_price_per_million=arguments.input_price,
            output_price_per_million=arguments.output_price,
            cache_price_per_million=arguments.cache_price,
        )
    except (OSError, ProfileConfigurationError, ValueError) as error:
        print(f"Authoring metadata update failed: {error}", file=sys.stderr)
        return 2
    print(
        "Authoring metadata saved: "
        f"profile={metadata.profile_id} context_window={metadata.context_window} "
        f"max_output_tokens={metadata.max_output_tokens} "
        f"configuration_fingerprint={metadata.configuration_fingerprint} "
        f"path={arguments.output.resolve()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
