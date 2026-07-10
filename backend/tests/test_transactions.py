from pathlib import Path

import pytest

from app.storage import transactions


def test_file_transaction_rolls_back_all_targets_on_write_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_path = tmp_path / "novel"
    (project_path / "book").mkdir(parents=True)
    first = project_path / "book" / "first.txt"
    second = project_path / "book" / "second.txt"
    first.write_text("old-first", encoding="utf-8")
    second.write_text("old-second", encoding="utf-8")
    original_promote = transactions._promote_staged_file
    promotion_count = 0

    def fail_second_promotion(staged: Path, target: Path, transaction_id: str) -> None:
        nonlocal promotion_count
        promotion_count += 1
        if promotion_count == 2:
            raise OSError("injected promotion failure")
        original_promote(staged, target, transaction_id)

    monkeypatch.setattr(transactions, "_promote_staged_file", fail_second_promotion)

    with pytest.raises(OSError, match="injected promotion failure"):
        transactions.commit_file_transaction(
            project_path,
            kind="test-rollback",
            files={
                "book/first.txt": "new-first",
                "book/second.txt": "new-second",
                "book/third.txt": "new-third",
            },
        )

    assert first.read_text(encoding="utf-8") == "old-first"
    assert second.read_text(encoding="utf-8") == "old-second"
    assert not (project_path / "book" / "third.txt").exists()
    assert not (project_path / "book" / ".transactions").exists()


def test_file_transaction_recovers_after_process_stops_mid_commit(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project_path = tmp_path / "novel"
    (project_path / "book").mkdir(parents=True)
    first = project_path / "book" / "first.txt"
    second = project_path / "book" / "second.txt"
    first.write_text("old-first", encoding="utf-8")
    second.write_text("old-second", encoding="utf-8")
    original_promote = transactions._promote_staged_file
    promotion_count = 0

    def stop_on_second_promotion(staged: Path, target: Path, transaction_id: str) -> None:
        nonlocal promotion_count
        promotion_count += 1
        if promotion_count == 2:
            raise SystemExit("simulated process stop")
        original_promote(staged, target, transaction_id)

    monkeypatch.setattr(transactions, "_promote_staged_file", stop_on_second_promotion)

    with pytest.raises(SystemExit, match="simulated process stop"):
        transactions.commit_file_transaction(
            project_path,
            kind="test-crash-recovery",
            files={
                "book/first.txt": "new-first",
                "book/second.txt": "new-second",
            },
        )

    assert first.read_text(encoding="utf-8") == "new-first"
    assert second.read_text(encoding="utf-8") == "old-second"

    transactions.recover_file_transactions(project_path)

    assert first.read_text(encoding="utf-8") == "old-first"
    assert second.read_text(encoding="utf-8") == "old-second"
    assert not (project_path / "book" / ".transactions").exists()
