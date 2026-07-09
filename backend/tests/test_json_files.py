from app.storage.json_files import read_json, write_json


def test_read_json_accepts_utf8_bom(tmp_path) -> None:
    path = tmp_path / "artifact.json"
    path.write_bytes(b'\xef\xbb\xbf{"schema_version": 1, "ok": true}')

    assert read_json(path) == {"schema_version": 1, "ok": True}


def test_write_json_does_not_write_utf8_bom(tmp_path) -> None:
    path = tmp_path / "artifact.json"

    write_json(path, {"schema_version": 1})

    assert not path.read_bytes().startswith(b"\xef\xbb\xbf")
