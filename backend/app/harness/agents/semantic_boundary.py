import json
from copy import deepcopy


_CONTROL_KEYS = frozenset(
    {
        "id",
        "schema_version",
        "version",
        "revision",
        "fingerprint",
        "target_file",
        "target_id",
        "file",
        "path",
        "source",
        "sha256",
        "evidence",
        "evidence_quote",
        "evidence_quotes",
        "candidate_evidence",
        "requires_commit",
        "checkpoint_id",
        "operation_index",
        "source_component_fingerprints",
    }
)


def semantic_model_value(value: object) -> object:
    """Return provider-safe semantic content without Harness control metadata."""

    if isinstance(value, dict):
        return {
            key: semantic_model_value(item)
            for key, item in value.items()
            if not is_harness_control_key(key)
        }
    if isinstance(value, list):
        return [semantic_model_value(item) for item in value]
    return deepcopy(value)


def semantic_model_text(value: str) -> str:
    """Sanitize structured text while preserving ordinary prose verbatim."""

    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return value
    if not isinstance(payload, dict | list):
        return value
    return json.dumps(semantic_model_value(payload), ensure_ascii=False, indent=2)


def is_harness_control_key(key: str) -> bool:
    normalized = key.casefold()
    if normalized in _CONTROL_KEYS:
        return True
    return (
        normalized.startswith("expected_")
        or normalized.startswith("based_on")
        or normalized.endswith("_id")
        or normalized.endswith("_ids")
        or normalized.endswith("_artifact")
        or normalized.endswith("_artifacts")
        or normalized.endswith("_revision")
        or normalized.endswith("_version")
        or normalized.endswith("_path")
        or normalized.endswith("_locator")
        or normalized.endswith("_fingerprint")
    )
