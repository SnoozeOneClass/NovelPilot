from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.storage.json_files import read_json, write_json


TRANSACTION_ROOT = Path("book") / ".transactions"


def commit_file_transaction(
    project_path: Path,
    *,
    kind: str,
    files: dict[str, str | bytes],
) -> None:
    if not files:
        return
    transaction_id = uuid4().hex[:16]
    root = project_path / TRANSACTION_ROOT / transaction_id
    staged_root = root / "staged"
    backup_root = root / "backup"
    manifest_path = root / "manifest.json"
    targets: list[dict[str, Any]] = []

    for relative_path, content in files.items():
        normalized = _normalize_relative_path(project_path, relative_path)
        target = project_path / normalized
        staged = staged_root / normalized
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)
        existed = target.exists()
        if existed:
            backup = backup_root / normalized
            backup.parent.mkdir(parents=True, exist_ok=True)
            backup.write_bytes(target.read_bytes())
        targets.append({"relative_path": normalized.as_posix(), "existed": existed})

    manifest = {
        "schema_version": 1,
        "transaction_id": transaction_id,
        "kind": kind,
        "status": "prepared",
        "created_at": datetime.now(UTC).isoformat(),
        "targets": targets,
    }
    write_json(manifest_path, manifest)

    try:
        for target_info in targets:
            relative = Path(target_info["relative_path"])
            _promote_staged_file(
                staged_root / relative,
                project_path / relative,
                transaction_id,
            )
        manifest["status"] = "committed"
        write_json(manifest_path, manifest)
    except Exception:
        try:
            _restore_targets(project_path, root, targets, transaction_id)
            manifest["status"] = "rolled_back"
            write_json(manifest_path, manifest)
        except Exception as rollback_exc:
            manifest["status"] = "rollback_failed"
            manifest["rollback_error"] = str(rollback_exc)
            write_json(manifest_path, manifest)
            raise RuntimeError(
                f"File transaction {transaction_id} failed and could not be rolled back."
            ) from rollback_exc
        finally:
            if manifest.get("status") == "rolled_back":
                shutil.rmtree(root, ignore_errors=True)
                _remove_empty_transaction_root(project_path)
        raise
    else:
        shutil.rmtree(root, ignore_errors=True)
        _remove_empty_transaction_root(project_path)


def recover_file_transactions(project_path: Path) -> None:
    root = project_path / TRANSACTION_ROOT
    if not root.exists():
        return
    for transaction_root in sorted(path for path in root.iterdir() if path.is_dir()):
        manifest_path = transaction_root / "manifest.json"
        if not manifest_path.exists():
            shutil.rmtree(transaction_root, ignore_errors=True)
            continue
        manifest = read_json(manifest_path, default={}) or {}
        status = manifest.get("status")
        targets = manifest.get("targets")
        if status == "prepared" and isinstance(targets, list):
            transaction_id = str(manifest.get("transaction_id") or transaction_root.name)
            _restore_targets(project_path, transaction_root, targets, transaction_id)
            manifest["status"] = "recovered_rollback"
            write_json(manifest_path, manifest)
        if manifest.get("status") in {"committed", "rolled_back", "recovered_rollback"}:
            shutil.rmtree(transaction_root, ignore_errors=True)
            continue
        raise RuntimeError(f"Unrecoverable file transaction: {transaction_root}")
    _remove_empty_transaction_root(project_path)


def _restore_targets(
    project_path: Path,
    transaction_root: Path,
    targets: list[dict[str, Any]],
    transaction_id: str,
) -> None:
    backup_root = transaction_root / "backup"
    for target_info in reversed(targets):
        relative = _normalize_relative_path(
            project_path,
            str(target_info["relative_path"]),
        )
        target = project_path / relative
        if bool(target_info.get("existed")):
            backup = backup_root / relative
            if not backup.exists():
                raise FileNotFoundError(f"Missing transaction backup: {backup}")
            _atomic_replace_bytes(target, backup.read_bytes(), transaction_id, "restore")
        elif target.exists():
            target.unlink()


def _promote_staged_file(staged: Path, target: Path, transaction_id: str) -> None:
    _atomic_replace_bytes(target, staged.read_bytes(), transaction_id, "promote")


def _atomic_replace_bytes(
    target: Path,
    content: bytes,
    transaction_id: str,
    phase: str,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{transaction_id}.{phase}.tmp")
    temporary.write_bytes(content)
    temporary.replace(target)


def _normalize_relative_path(project_path: Path, value: str) -> Path:
    if not value or "\\" in value:
        raise ValueError(f"Invalid transaction target: {value}")
    relative = Path(value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(f"Invalid transaction target: {value}")
    project_root = project_path.resolve()
    target = (project_root / relative).resolve()
    try:
        target.relative_to(project_root)
    except ValueError as exc:
        raise ValueError(f"Transaction target escapes project: {value}") from exc
    return relative


def _remove_empty_transaction_root(project_path: Path) -> None:
    try:
        (project_path / TRANSACTION_ROOT).rmdir()
    except OSError:
        pass
