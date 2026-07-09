from pathlib import Path

from app.core.config import OUTPUT_DIR

WINDOWS_FORBIDDEN_CHARS = set('<>:"/\\|?*')
ARTIFACT_PATH_FORBIDDEN_CHARS = set('<>:"|?*')


def sanitize_project_name(name: str) -> str:
    if "/" in name or "\\" in name:
        raise ValueError("Project name cannot contain path separators.")
    cleaned = "".join("_" if char in WINDOWS_FORBIDDEN_CHARS else char for char in name.strip())
    cleaned = cleaned.strip(" .")
    if not cleaned:
        raise ValueError("Project name cannot be empty.")
    if cleaned in {".", ".."} or ".." in Path(cleaned).parts:
        raise ValueError("Project name cannot contain parent path references.")
    return cleaned


def resolve_project_path(name: str) -> Path:
    project_name = sanitize_project_name(name)
    project_path = (OUTPUT_DIR / project_name).resolve()
    output_root = OUTPUT_DIR.resolve()
    try:
        project_path.relative_to(output_root)
    except ValueError as exc:
        raise ValueError("Project path must stay inside output/.") from exc
    return project_path


def ensure_relative_artifact_path(path: str) -> Path:
    stripped_path = path.strip()
    if not stripped_path or stripped_path in {".", ".."} or "\\" in path:
        raise ValueError("Artifact path must be a safe project-relative POSIX path.")
    artifact_path = Path(path)
    if (
        artifact_path.is_absolute()
        or artifact_path.drive
        or artifact_path.root
        or ".." in artifact_path.parts
        or any(_is_unsafe_artifact_path_part(part) for part in artifact_path.parts)
    ):
        raise ValueError("Artifact path must be relative and stay inside the project.")
    return artifact_path


def _is_unsafe_artifact_path_part(part: str) -> bool:
    return (
        not part
        or part in {".", ".."}
        or part != part.strip(" ")
        or part.endswith(".")
        or any(char in ARTIFACT_PATH_FORBIDDEN_CHARS for char in part)
    )


def resolve_artifact_path(project_path: Path, path: str) -> Path:
    relative_path = ensure_relative_artifact_path(path)
    artifact_path = (project_path / relative_path).resolve()
    project_root = project_path.resolve()
    try:
        artifact_path.relative_to(project_root)
    except ValueError as exc:
        raise ValueError("Artifact path must stay inside the active project.") from exc
    return artifact_path
