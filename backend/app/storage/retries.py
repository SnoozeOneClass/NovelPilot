from pathlib import Path

from app.storage.json_files import read_json

VERIFICATION_RETRY_ARTIFACTS = ["draft.md", "observations.json", "review.md", "verification.json"]
STATE_PATCH_RETRY_ARTIFACTS = ["state_patch_rejection.json"]


def retry_scope_for_chapter(chapter_path: Path) -> tuple[str | None, list[str]]:
    if (chapter_path / "state_patch_rejection.json").exists() and not (
        chapter_path / "committed_state_patch.json"
    ).exists():
        return "state_patch", STATE_PATCH_RETRY_ARTIFACTS

    verification_path = chapter_path / "verification.json"
    if verification_path.exists() and not (chapter_path / "final.md").exists():
        payload = read_json(verification_path, default={})
        verification = payload if isinstance(payload, dict) else {}
        if verification.get("commit_allowed") is False:
            return "chapter_candidate", VERIFICATION_RETRY_ARTIFACTS

    return None, []
