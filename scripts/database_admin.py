from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_DIR = ROOT_DIR / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from alembic import command  # noqa: E402

from app.core.config import DATABASE_BACKUP_DIR, DATABASE_PATH  # noqa: E402
from app.db.maintenance import (  # noqa: E402
    alembic_config,
    create_consistent_backup,
    restore_database,
    validate_backup,
    validate_database,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="NovelPilot SQLite maintenance")
    subparsers = parser.add_subparsers(dest="operation", required=True)

    migrate = subparsers.add_parser("migrate", help="Upgrade an application database to head")
    migrate.add_argument("--database", type=Path, default=DATABASE_PATH)

    check = subparsers.add_parser("check", help="Check schema drift and database health")
    check.add_argument("--database", type=Path, default=DATABASE_PATH)

    subparsers.add_parser(
        "migrate-test",
        help="Run the empty-database upgrade/downgrade/upgrade migration gate",
    )

    backup = subparsers.add_parser("backup", help="Create a consistent full-database snapshot")
    backup.add_argument("--database", type=Path, default=DATABASE_PATH)
    backup.add_argument(
        "--destination",
        type=Path,
        default=DATABASE_BACKUP_DIR / "novelpilot-backup.sqlite3",
    )

    validate = subparsers.add_parser("validate-backup", help="Verify a backup and manifest")
    validate.add_argument("backup", type=Path)

    restore = subparsers.add_parser("restore", help="Restore while the service is stopped")
    restore.add_argument("backup", type=Path)
    restore.add_argument("--database", type=Path, default=DATABASE_PATH)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.operation == "migrate":
        command.upgrade(alembic_config(arguments.database), "head")
        result: object = asdict(validate_database(arguments.database))
    elif arguments.operation == "check":
        command.check(alembic_config(arguments.database))
        result = asdict(validate_database(arguments.database))
    elif arguments.operation == "migrate-test":
        with tempfile.TemporaryDirectory(prefix="novelpilot-migration-") as directory:
            database_path = Path(directory) / "migration.sqlite3"
            config = alembic_config(database_path)
            command.upgrade(config, "head")
            command.check(config)
            command.downgrade(config, "base")
            command.upgrade(config, "head")
            command.check(config)
            result = asdict(validate_database(database_path))
    elif arguments.operation == "backup":
        result = asdict(
            create_consistent_backup(arguments.database, arguments.destination)
        )
    elif arguments.operation == "validate-backup":
        manifest, health = validate_backup(arguments.backup)
        result = {"manifest": asdict(manifest), "health": asdict(health)}
    elif arguments.operation == "restore":
        result = asdict(restore_database(arguments.backup, arguments.database))
    else:  # pragma: no cover - argparse prevents this.
        raise AssertionError(f"Unknown operation: {arguments.operation}")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
