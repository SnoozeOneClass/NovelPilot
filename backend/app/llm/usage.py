from typing import Any


def merge_usage(
    accumulated: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    """Recursively aggregate provider usage without assuming one token vocabulary."""
    merged = dict(accumulated)
    for key, value in current.items():
        previous = merged.get(key)
        if isinstance(value, dict):
            nested = previous if isinstance(previous, dict) else {}
            merged[key] = merge_usage(nested, value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            prior_number = (
                previous
                if isinstance(previous, (int, float)) and not isinstance(previous, bool)
                else 0
            )
            merged[key] = prior_number + value
        else:
            merged[key] = value
    return merged
