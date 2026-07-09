from pathlib import Path

from app.storage.text_files import read_text_file, write_text_file


def export_manuscript(project_path: Path) -> Path:
    chapters_path = project_path / "chapters"
    exports_path = project_path / "exports"
    exports_path.mkdir(parents=True, exist_ok=True)
    manuscript_path = exports_path / "manuscript.md"

    parts: list[str] = []
    if chapters_path.exists():
        for chapter_dir in sorted(item for item in chapters_path.iterdir() if item.is_dir()):
            final_path = chapter_dir / "final.md"
            if final_path.exists():
                parts.append(read_text_file(final_path).strip())

    write_text_file(manuscript_path, "\n\n".join(part for part in parts if part) + "\n")
    return manuscript_path
