from pathlib import Path


def read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def write_text_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(path)
