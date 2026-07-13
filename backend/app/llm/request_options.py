from typing import Any


PROTECTED_REQUEST_FIELDS = {"model", "messages", "system", "stream"}


def merge_request_options(
    base_payload: dict[str, Any],
    profile_options: dict[str, Any],
    request_options: dict[str, Any],
) -> dict[str, Any]:
    """Merge provider-specific body fields without losing the assembled conversation."""

    payload = {**base_payload, **profile_options, **request_options}
    for field in PROTECTED_REQUEST_FIELDS:
        if field in base_payload:
            payload[field] = base_payload[field]
    return {key: value for key, value in payload.items() if value is not None}
