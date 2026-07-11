from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence

from app.core import config as core_config
from app.schemas.profiles import LlmProfile
from app.storage import profiles as profile_storage

SecretKind = Literal["api_key", "base_url"]
AuditStatus = Literal["passed", "failed"]
SCAN_RETRY_DELAYS_SECONDS = (0.05, 0.1)


@dataclass(frozen=True)
class SecretAuditFinding:
    path: str
    profile_id: str
    kind: SecretKind

    def to_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "profile_id": self.profile_id,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class SecretAuditResult:
    status: AuditStatus
    root_path: str
    profile_count: int
    scanned_file_count: int
    findings: list[SecretAuditFinding]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "root_path": self.root_path,
            "profile_count": self.profile_count,
            "scanned_file_count": self.scanned_file_count,
            "findings": [finding.to_dict() for finding in self.findings],
        }


@dataclass(frozen=True)
class _SensitiveNeedle:
    profile_id: str
    kind: SecretKind
    value: str


def audit_output_for_profile_secrets(output_dir: Path | None = None) -> SecretAuditResult:
    return audit_path_for_profile_secrets(output_dir or core_config.OUTPUT_DIR)


def audit_path_for_profile_secrets(
    root_path: Path,
    profiles: Sequence[LlmProfile] | None = None,
) -> SecretAuditResult:
    profiles_to_check = list(profiles) if profiles is not None else profile_storage.load_profiles().profiles
    needles = _profile_secret_needles(profiles_to_check)
    findings: list[SecretAuditFinding]
    scanned_file_count = 0

    for attempt in range(len(SCAN_RETRY_DELAYS_SECONDS) + 1):
        try:
            scanned_file_count, findings = _scan_path_for_needles(root_path, needles)
            break
        except (FileNotFoundError, PermissionError):
            if attempt >= len(SCAN_RETRY_DELAYS_SECONDS):
                raise
            time.sleep(SCAN_RETRY_DELAYS_SECONDS[attempt])

    return SecretAuditResult(
        status="failed" if findings else "passed",
        root_path=str(root_path),
        profile_count=len(profiles_to_check),
        scanned_file_count=scanned_file_count,
        findings=findings,
    )


def _scan_path_for_needles(
    root_path: Path,
    needles: Sequence[_SensitiveNeedle],
) -> tuple[int, list[SecretAuditFinding]]:
    findings: list[SecretAuditFinding] = []
    seen_findings: set[tuple[str, str, SecretKind]] = set()
    scanned_file_count = 0

    if root_path.exists():
        for path in root_path.rglob("*"):
            if not path.is_file():
                continue
            scanned_file_count += 1
            text = _read_audited_text(path)
            relative_path = path.relative_to(root_path).as_posix()
            safe_relative_path = _redact_sensitive_values(relative_path, needles)
            for needle in needles:
                if needle.value not in text:
                    continue
                finding_key = (relative_path, needle.profile_id, needle.kind)
                if finding_key in seen_findings:
                    continue
                seen_findings.add(finding_key)
                findings.append(
                    SecretAuditFinding(
                        path=safe_relative_path,
                        profile_id=needle.profile_id,
                        kind=needle.kind,
                    )
                )

    return scanned_file_count, findings


def _read_audited_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except PermissionError:
        if not path.name.endswith(".lock"):
            raise

    # exclusive_file_lock reserves byte zero. Windows prevents reads that touch
    # that byte while the lock is held, but the remainder is still readable.
    # Fall back to scanning from byte one so concurrent readiness checks stay
    # reliable; unlocked lock files still take the normal full-file path above.
    with path.open("rb") as handle:
        handle.seek(1)
        return handle.read().decode("utf-8", errors="ignore")


def _profile_secret_needles(profiles: Sequence[LlmProfile]) -> list[_SensitiveNeedle]:
    needles: list[_SensitiveNeedle] = []
    for profile in profiles:
        api_key = profile.api_key.get_secret_value()
        if api_key:
            needles.append(_SensitiveNeedle(profile.id, "api_key", api_key))
        base_url = str(profile.base_url)
        for value in {base_url, base_url.rstrip("/")}:
            if value:
                needles.append(_SensitiveNeedle(profile.id, "base_url", value))
    return needles


def _redact_sensitive_values(text: str, needles: Sequence[_SensitiveNeedle]) -> str:
    redacted = text
    for needle in sorted(needles, key=lambda item: len(item.value), reverse=True):
        redacted = redacted.replace(needle.value, "[redacted]")
    return redacted
