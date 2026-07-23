from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from app.profiles import ProfilesDocument

SCAN_CHUNK_SIZE = 1024 * 1024


class SecretAuditError(RuntimeError):
    """A runtime path could not be completely and safely audited."""


@dataclass(frozen=True, slots=True)
class SecretAuditFinding:
    path: str
    profile_id: str
    kind: str = "api_key"


@dataclass(frozen=True, slots=True)
class SecretAuditResult:
    status: str
    roots: tuple[str, ...]
    profile_count: int
    scanned_file_count: int
    findings: tuple[SecretAuditFinding, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "roots": list(self.roots),
            "profile_count": self.profile_count,
            "scanned_file_count": self.scanned_file_count,
            "findings": [asdict(item) for item in self.findings],
        }


def _profile_secrets(document: ProfilesDocument) -> tuple[tuple[str, bytes], ...]:
    values: list[tuple[str, bytes]] = []
    for profile in document.profiles:
        secret = profile.api_key.get_secret_value().encode("utf-8")
        if secret:
            values.append((profile.id, secret))
    return tuple(values)


def _contains_needles(path: Path, needles: tuple[bytes, ...]) -> set[bytes]:
    if not needles:
        return set()
    longest = max(len(value) for value in needles)
    overlap = b""
    found: set[bytes] = set()
    with path.open("rb") as handle:
        while block := handle.read(SCAN_CHUNK_SIZE):
            window = overlap + block
            found.update(value for value in needles if value in window)
            if len(found) == len(set(needles)):
                break
            overlap = window[-(longest - 1) :] if longest > 1 else b""
    return found


def _redacted_relative_path(path: Path, root: Path, secrets: Iterable[bytes]) -> str:
    value = path.relative_to(root).as_posix()
    for secret in secrets:
        value = value.replace(secret.decode("utf-8", errors="ignore"), "[redacted]")
    return value


def audit_runtime_paths(
    *,
    roots: Iterable[Path],
    profiles: ProfilesDocument,
) -> SecretAuditResult:
    secret_pairs = _profile_secrets(profiles)
    secret_values = tuple(secret for _profile_id, secret in secret_pairs)
    unique_needles = tuple(dict.fromkeys(secret_values))
    resolved_roots = tuple(dict.fromkeys(path.resolve() for path in roots))
    findings: list[SecretAuditFinding] = []
    scanned = 0
    for root in resolved_roots:
        if not root.exists():
            continue
        if not root.is_dir():
            raise SecretAuditError(f"Audit root is not a directory: {root}")
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(root):
                raise SecretAuditError(f"Audit path escapes its root: {path}")
            try:
                found = _contains_needles(resolved, unique_needles)
            except OSError as exc:
                raise SecretAuditError(f"Cannot read audited file: {path}") from exc
            scanned += 1
            safe_path = _redacted_relative_path(resolved, root, secret_values)
            root_label = root.name or str(root)
            for profile_id, secret in secret_pairs:
                if secret in found:
                    findings.append(
                        SecretAuditFinding(
                            path=f"{root_label}/{safe_path}",
                            profile_id=profile_id,
                        )
                    )
    return SecretAuditResult(
        status="failed" if findings else "passed",
        roots=tuple(root.name or str(root) for root in resolved_roots),
        profile_count=len(secret_pairs),
        scanned_file_count=scanned,
        findings=tuple(findings),
    )
