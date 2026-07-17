from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.core import config
from app.core.paths import resolve_artifact_path
from app.harness.run_control import has_active_runner
from app.schemas.arcs import CurrentArcState
from app.schemas.experiments import (
    ExperimentFixtureCheckpoint,
    ExperimentFixtureCreateResponse,
    ExperimentFixtureFile,
    ExperimentFixtureIssue,
    ExperimentFixtureManifest,
    ExperimentFixtureStatus,
    ExperimentFixtureSummary,
)
from app.storage import arcs as arc_storage
from app.storage.file_lock import exclusive_file_lock
from app.storage.json_files import read_json
from app.storage.projects import read_project_metadata
from app.storage.setup import read_setup_state


FIXTURE_VERSION = "fixture-v1"
REQUIRED_BOOK_FILES = (
    "book/direction.md",
    "book/constraints.json",
    "book/settings.md",
    "book/outline.md",
    "book/state.json",
)
OPTIONAL_BOOK_FILES = ("book/feedback.md",)
CANON_FILES = (
    "canon/characters.json",
    "canon/relationships.json",
    "canon/world_facts.json",
    "canon/foreshadowing.json",
)
ARC_FILES = ("plan.md", "state.json")
OPTIONAL_ARC_FILES = ("revision.md",)
CHAPTER_FILES = ("final.md", "committed_state_patch.json")


class ExperimentFixtureIneligibleError(ValueError):
    def __init__(self, issues: list[ExperimentFixtureIssue]) -> None:
        super().__init__("; ".join(issue.message for issue in issues))
        self.issues = issues


class ExperimentFixtureIntegrityError(ValueError):
    pass


@dataclass(frozen=True)
class _EligibleSource:
    checkpoint: ExperimentFixtureCheckpoint
    source_files: dict[str, bytes]


def get_fixture_status(
    project_path: Path,
    *,
    ignore_active_runner: bool = False,
    allow_pending_current_arc_review: bool = False,
) -> ExperimentFixtureStatus:
    metadata = read_project_metadata(project_path)
    inspection = _inspect_source(
        project_path,
        ignore_active_runner=ignore_active_runner,
        allow_pending_current_arc_review=allow_pending_current_arc_review,
    )
    if isinstance(inspection, list):
        return ExperimentFixtureStatus(
            project_kind=metadata.project_kind,
            lifecycle=metadata.benchmark_fixture,
            eligible=False,
            issues=inspection,
        )

    existing = _find_matching_fixture(inspection.checkpoint)
    return ExperimentFixtureStatus(
        project_kind=metadata.project_kind,
        lifecycle=metadata.benchmark_fixture,
        eligible=True,
        checkpoint=inspection.checkpoint,
        existing_fixture=existing,
    )


def create_fixture(
    project_path: Path,
    *,
    ignore_active_runner: bool = False,
) -> ExperimentFixtureCreateResponse:
    inspection = _inspect_source(
        project_path,
        ignore_active_runner=ignore_active_runner,
        allow_pending_current_arc_review=False,
    )
    if isinstance(inspection, list):
        raise ExperimentFixtureIneligibleError(inspection)

    root = _experiment_root()
    fixtures_root = root / "fixtures"
    creating_root = root / ".creating"
    with exclusive_file_lock(root / ".fixtures.lock"):
        existing = _find_matching_fixture(inspection.checkpoint)
        if existing is not None:
            return ExperimentFixtureCreateResponse(created=False, fixture=existing)

        fixture_id = f"fixture-{uuid4()}"
        staging_path = creating_root / fixture_id
        fixture_path = fixtures_root / fixture_id
        if staging_path.exists() or fixture_path.exists():
            raise FileExistsError(f"Experiment fixture already exists: {fixture_id}")

        try:
            staging_path.mkdir(parents=True)
            payloads = _fixture_payloads(inspection)
            file_entries = _write_payloads(staging_path, payloads)
            manifest = ExperimentFixtureManifest(
                fixture_id=fixture_id,
                created_at=datetime.now(UTC),
                checkpoint=inspection.checkpoint,
                files=file_entries,
            )
            manifest_bytes = _json_document(manifest.model_dump(mode="json"))
            (staging_path / "manifest.json").write_bytes(manifest_bytes)
            (staging_path / "manifest.sha256").write_text(
                _sha256(manifest_bytes) + "\n",
                encoding="utf-8",
            )
            verify_fixture(staging_path)
            fixtures_root.mkdir(parents=True, exist_ok=True)
            staging_path.replace(fixture_path)
        except BaseException:
            shutil.rmtree(staging_path, ignore_errors=True)
            _remove_empty_directory(creating_root)
            raise
        _remove_empty_directory(creating_root)

    return ExperimentFixtureCreateResponse(
        created=True,
        fixture=_fixture_summary(fixture_path, manifest),
    )


def verify_fixture(fixture_path: Path) -> ExperimentFixtureManifest:
    manifest_path = fixture_path / "manifest.json"
    checksum_path = fixture_path / "manifest.sha256"
    if not manifest_path.is_file() or not checksum_path.is_file():
        raise ExperimentFixtureIntegrityError("Fixture manifest or checksum is missing.")

    manifest_bytes = manifest_path.read_bytes()
    expected_manifest_hash = checksum_path.read_text(encoding="utf-8").strip()
    if expected_manifest_hash != _sha256(manifest_bytes):
        raise ExperimentFixtureIntegrityError("Fixture manifest checksum does not match.")

    try:
        manifest = ExperimentFixtureManifest.model_validate_json(manifest_bytes)
    except ValueError as exc:
        raise ExperimentFixtureIntegrityError("Fixture manifest is invalid.") from exc
    if manifest.fixture_id != fixture_path.name:
        raise ExperimentFixtureIntegrityError(
            "Fixture manifest identifier does not match its directory."
        )

    expected_paths = {entry.path for entry in manifest.files}
    if len(expected_paths) != len(manifest.files):
        raise ExperimentFixtureIntegrityError("Fixture manifest contains duplicate paths.")
    actual_paths = {
        path.relative_to(fixture_path).as_posix()
        for path in fixture_path.rglob("*")
        if path.is_file()
    }
    actual_paths.discard("manifest.json")
    actual_paths.discard("manifest.sha256")
    if actual_paths != expected_paths:
        raise ExperimentFixtureIntegrityError("Fixture payload file set does not match manifest.")

    for entry in manifest.files:
        payload_path = _safe_fixture_payload_path(fixture_path, entry.path)
        content = payload_path.read_bytes()
        if len(content) != entry.byte_size or _sha256(content) != entry.sha256:
            raise ExperimentFixtureIntegrityError(
                f"Fixture payload integrity check failed: {entry.path}"
            )
    return manifest


def _inspect_source(
    project_path: Path,
    *,
    ignore_active_runner: bool,
    allow_pending_current_arc_review: bool,
) -> _EligibleSource | list[ExperimentFixtureIssue]:
    issues: list[ExperimentFixtureIssue] = []
    metadata = read_project_metadata(project_path)
    setup_state = read_setup_state(project_path)

    if not setup_state.approved:
        _issue(issues, "book_not_approved", "请先批准全书方向。")
    for relative_path in REQUIRED_BOOK_FILES:
        if not (project_path / relative_path).is_file():
            _issue(
                issues,
                "missing_book_artifact",
                f"缺少已批准的全书产物：{relative_path}。",
            )

    if metadata.run_status in {"running", "pause_requested"}:
        _issue(issues, "run_in_flight", "Harness 正在运行或等待安全暂停，暂时不能冻结。")
    if not ignore_active_runner and has_active_runner(project_path):
        _issue(issues, "active_runner", "当前项目仍有活动中的 Harness 请求。")
    if metadata.active_chapter_id is not None:
        _issue(issues, "active_chapter", "当前故事弧已经开始生成章节，不能作为起始母本。")
    if metadata.active_arc_id is None:
        _issue(issues, "missing_current_arc", "请先生成并批准待测试的故事弧计划。")
    elif not _has_numeric_id(metadata.active_arc_id, "arc"):
        _issue(issues, "invalid_current_arc_id", "当前故事弧标识无法验证。")

    current_arc: CurrentArcState | None = None
    if metadata.active_arc_id is not None:
        try:
            current_arc = arc_storage.read_arc_state(project_path, metadata.active_arc_id)
        except ValueError:
            _issue(issues, "invalid_current_arc", "当前故事弧状态无法验证。")
        if current_arc is None:
            _issue(issues, "missing_current_arc_state", "缺少当前故事弧状态文件。")
        elif current_arc.arc_id != metadata.active_arc_id:
            _issue(issues, "current_arc_mismatch", "当前故事弧与项目元数据不一致。")
        else:
            if current_arc.human_review != "approved" and not (
                allow_pending_current_arc_review
                and current_arc.human_review == "awaiting_review"
            ):
                _issue(issues, "current_arc_not_approved", "请先明确批准当前故事弧计划。")
            if current_arc.completed_chapter_ids:
                _issue(issues, "current_arc_started", "当前故事弧已经提交章节，冻结点已经过去。")

    completed_arcs: list[CurrentArcState] = []
    arcs_root = project_path / "arcs"
    if arcs_root.exists():
        for arc_path in sorted(arcs_root.iterdir(), key=lambda path: _numeric_id(path.name)):
            if not arc_path.is_dir() or not _has_numeric_id(arc_path.name, "arc"):
                continue
            try:
                state = arc_storage.read_arc_state(project_path, arc_path.name)
            except ValueError:
                _issue(issues, "invalid_arc_state", f"故事弧状态无法验证：{arc_path.name}。")
                continue
            if state is None or state.arc_id == metadata.active_arc_id:
                continue
            if (
                state.status == "completed"
                and _has_numeric_id(state.arc_id, "arc")
                and _numeric_id(state.arc_id) < _numeric_id(metadata.active_arc_id or "")
            ):
                completed_arcs.append(state)

    if not completed_arcs:
        _issue(issues, "missing_warmup_arc", "至少需要一个已经完成的共享预热故事弧。")

    warmup_chapter_ids = sorted(
        {
            chapter_id
            for arc in completed_arcs
            for chapter_id in arc.completed_chapter_ids
        },
        key=_numeric_id,
    )
    for arc in completed_arcs:
        if arc.completed_at is None or not arc.completed_chapter_ids:
            _issue(issues, "incomplete_warmup_arc", f"预热故事弧提交记录不完整：{arc.arc_id}。")
    for chapter_id in warmup_chapter_ids:
        if not _has_numeric_id(chapter_id, "chapter"):
            _issue(issues, "invalid_chapter_id", f"预热章节标识无法验证：{chapter_id}。")
            continue
        for filename in CHAPTER_FILES:
            relative_path = f"chapters/{chapter_id}/{filename}"
            if not (project_path / relative_path).is_file():
                _issue(issues, "missing_committed_chapter", f"缺少已提交章节产物：{relative_path}。")

    for relative_path in CANON_FILES:
        payload = read_json(project_path / relative_path, default=None)
        if not isinstance(payload, dict):
            _issue(issues, "invalid_canon", f"正史文件缺失或无法验证：{relative_path}。")

    if issues or current_arc is None:
        return issues

    completed_arc_ids = sorted((arc.arc_id for arc in completed_arcs), key=_numeric_id)
    source_paths = _source_paths(
        project_path,
        completed_arc_ids=completed_arc_ids,
        current_arc_id=current_arc.arc_id,
        warmup_chapter_ids=warmup_chapter_ids,
    )
    try:
        source_files = {
            relative_path: _read_source_file(project_path, relative_path)
            for relative_path in source_paths
        }
    except (FileNotFoundError, UnicodeError, ValueError) as exc:
        return [
            ExperimentFixtureIssue(
                code="unreadable_source_artifact",
                message=f"母本来源文件无法安全读取：{exc}",
            )
        ]
    fingerprint = _checkpoint_fingerprint(
        source_project_id=metadata.project_id,
        active_arc_id=current_arc.arc_id,
        source_files=source_files,
    )
    checkpoint = ExperimentFixtureCheckpoint(
        source_project_name=project_path.name,
        source_project_id=metadata.project_id,
        source_title=metadata.title,
        active_arc_id=current_arc.arc_id,
        completed_arc_ids=completed_arc_ids,
        warmup_chapter_ids=warmup_chapter_ids,
        recommended_target_chapter_count=current_arc.recommended_target_chapter_count,
        target_chapter_count=current_arc.target_chapter_count,
        checkpoint_fingerprint=fingerprint,
    )
    return _EligibleSource(checkpoint=checkpoint, source_files=source_files)


def _source_paths(
    project_path: Path,
    *,
    completed_arc_ids: list[str],
    current_arc_id: str,
    warmup_chapter_ids: list[str],
) -> list[str]:
    paths = [*REQUIRED_BOOK_FILES, *CANON_FILES]
    paths.extend(
        relative_path
        for relative_path in OPTIONAL_BOOK_FILES
        if (project_path / relative_path).is_file()
    )
    for arc_id in sorted([*completed_arc_ids, current_arc_id], key=_numeric_id):
        paths.extend(f"arcs/{arc_id}/{filename}" for filename in ARC_FILES)
        paths.extend(
            f"arcs/{arc_id}/{filename}"
            for filename in OPTIONAL_ARC_FILES
            if (project_path / "arcs" / arc_id / filename).is_file()
        )
    for chapter_id in warmup_chapter_ids:
        paths.extend(f"chapters/{chapter_id}/{filename}" for filename in CHAPTER_FILES)
    return sorted(set(paths))


def _fixture_payloads(source: _EligibleSource) -> dict[str, bytes]:
    payloads = {
        f"snapshot/{relative_path}": content
        for relative_path, content in source.source_files.items()
    }
    payloads["direct_prompt.md"] = _render_direct_prompt(source).encode("utf-8")
    return payloads


def _render_direct_prompt(source: _EligibleSource) -> str:
    files = source.source_files
    sections = [
        "# Direct-v1 实验母本上下文",
        "",
        "以下内容来自同一冻结检查点。请仅根据这些批准产物、已提交正史和共享前文继续写作。",
        "",
    ]
    ordered_sections: list[tuple[str, str]] = [
        ("已批准全书方向", "book/direction.md"),
        ("结构化全书约束", "book/constraints.json"),
        ("全书设定", "book/settings.md"),
        ("滚动故事弧契约", "book/outline.md"),
        ("全书状态", "book/state.json"),
    ]
    if "book/feedback.md" in files:
        ordered_sections.append(("全书反馈备忘", "book/feedback.md"))
    for arc_id in sorted(
        [*source.checkpoint.completed_arc_ids, source.checkpoint.active_arc_id],
        key=_numeric_id,
    ):
        ordered_sections.extend(
            [
                (f"{arc_id} 计划", f"arcs/{arc_id}/plan.md"),
                (f"{arc_id} 状态", f"arcs/{arc_id}/state.json"),
            ]
        )
    ordered_sections.extend(
        [
            ("角色正史", "canon/characters.json"),
            ("关系正史", "canon/relationships.json"),
            ("世界事实正史", "canon/world_facts.json"),
            ("伏笔正史", "canon/foreshadowing.json"),
        ]
    )
    for chapter_id in source.checkpoint.warmup_chapter_ids:
        ordered_sections.append((f"共享前文 {chapter_id}", f"chapters/{chapter_id}/final.md"))

    for title, relative_path in ordered_sections:
        sections.extend(
            [
                f"## {title}",
                "",
                _decode_source(files[relative_path]),
                "",
            ]
        )
    return "\n".join(sections).rstrip() + "\n"


def _write_payloads(root: Path, payloads: dict[str, bytes]) -> list[ExperimentFixtureFile]:
    entries: list[ExperimentFixtureFile] = []
    for relative_path, content in sorted(payloads.items()):
        target = _safe_fixture_payload_path(root, relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        entries.append(
            ExperimentFixtureFile(
                path=relative_path,
                byte_size=len(content),
                sha256=_sha256(content),
            )
        )
    return entries


def _find_matching_fixture(
    checkpoint: ExperimentFixtureCheckpoint,
) -> ExperimentFixtureSummary | None:
    fixtures_root = _experiment_root() / "fixtures"
    if not fixtures_root.exists():
        return None
    for fixture_path in sorted(fixtures_root.iterdir(), key=lambda path: path.name):
        if not fixture_path.is_dir() or not fixture_path.name.startswith("fixture-"):
            continue
        try:
            manifest = verify_fixture(fixture_path)
        except (OSError, ValueError):
            continue
        if (
            manifest.checkpoint.source_project_id == checkpoint.source_project_id
            and manifest.checkpoint.checkpoint_fingerprint
            == checkpoint.checkpoint_fingerprint
        ):
            return _fixture_summary(fixture_path, manifest)
    return None


def _fixture_summary(
    fixture_path: Path,
    manifest: ExperimentFixtureManifest,
) -> ExperimentFixtureSummary:
    return ExperimentFixtureSummary(
        fixture_version=manifest.fixture_version,
        integrity_verified=True,
        fixture_id=manifest.fixture_id,
        created_at=manifest.created_at,
        relative_path=fixture_path.relative_to(config.OUTPUT_DIR).as_posix(),
        checkpoint=manifest.checkpoint,
    )


def _read_source_file(project_path: Path, relative_path: str) -> bytes:
    source_path = resolve_artifact_path(project_path, relative_path)
    if not source_path.is_file():
        raise FileNotFoundError(relative_path)
    content = source_path.read_bytes()
    _decode_source(content)
    return content


def _safe_fixture_payload_path(root: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    if (
        not relative_path
        or "\\" in relative_path
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ExperimentFixtureIntegrityError(f"Unsafe fixture payload path: {relative_path}")
    target = (root / relative).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError as exc:
        raise ExperimentFixtureIntegrityError(
            f"Fixture payload path escapes fixture root: {relative_path}"
        ) from exc
    return target


def _checkpoint_fingerprint(
    *,
    source_project_id: str,
    active_arc_id: str,
    source_files: dict[str, bytes],
) -> str:
    digest = hashlib.sha256()
    for value in [FIXTURE_VERSION, source_project_id, active_arc_id]:
        digest.update(value.encode("utf-8"))
        digest.update(b"\0")
    for relative_path, content in sorted(source_files.items()):
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256(content).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _json_document(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _decode_source(content: bytes) -> str:
    return content.decode("utf-8-sig").strip()


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _experiment_root() -> Path:
    return config.OUTPUT_DIR / "experiments"


def _numeric_id(value: str) -> int:
    try:
        prefix, number = value.rsplit("-", 1)
        if prefix not in {"arc", "chapter"}:
            return -1
        return int(number)
    except (TypeError, ValueError):
        return -1


def _has_numeric_id(value: str, prefix: str) -> bool:
    try:
        actual_prefix, number = value.rsplit("-", 1)
        return actual_prefix == prefix and number.isdigit()
    except (AttributeError, ValueError):
        return False


def _issue(
    issues: list[ExperimentFixtureIssue],
    code: str,
    message: str,
) -> None:
    if not any(issue.code == code and issue.message == message for issue in issues):
        issues.append(ExperimentFixtureIssue(code=code, message=message))


def _remove_empty_directory(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass
