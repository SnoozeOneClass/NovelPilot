from app.storage.text_files import read_text_file, write_text_file


def test_read_text_file_accepts_utf8_bom(tmp_path) -> None:
    path = tmp_path / "artifact.md"
    path.write_bytes(b"\xef\xbb\xbf# Title\n")

    assert read_text_file(path) == "# Title\n"


def test_write_text_file_replaces_existing_file(tmp_path) -> None:
    path = tmp_path / "nested" / "artifact.md"

    write_text_file(path, "first\n")
    write_text_file(path, "second\n")

    assert path.read_text(encoding="utf-8") == "second\n"
    assert not (path.with_name(path.name + ".tmp")).exists()
