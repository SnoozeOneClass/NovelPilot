from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.core.config import LLM_PROFILES_PATH  # noqa: E402
from app.profiles import encode_profiles_document, migrate_legacy_profiles  # noqa: E402


def main() -> int:
    if not LLM_PROFILES_PATH.exists():
        print("Profile config does not exist; nothing to migrate.")
        return 0
    raw = json.loads(LLM_PROFILES_PATH.read_text(encoding="utf-8"))
    if raw.get("schema_version") == 2:
        print("Profile config is already schema version 2.")
        return 0
    encoded = encode_profiles_document(migrate_legacy_profiles(raw))
    temporary = LLM_PROFILES_PATH.with_suffix(LLM_PROFILES_PATH.suffix + ".tmp")
    temporary.write_bytes(encoded)
    os.replace(temporary, LLM_PROFILES_PATH)
    print("Migrated local profile config to schema version 2; secrets were retained in place.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
