from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from hashlib import file_digest
from pathlib import Path
from typing import Any, TypedDict

from app.authoring.domain.models import content_hash


class ReportError(TypedDict):
    stage: str
    error_type: str


class FailureArtifacts(TypedDict):
    database: str
    diagnostics: str
    report_json: str
    report_markdown: str
    manuscript: str
    first_failure: dict[str, Any] | None


# These errors are raised by the harness using domain identifiers and bounded counters,
# rather than Provider response bodies or Pydantic validation inputs.
_INTERNAL_ERRORS = {
    "ActivationRequestBudgetExhausted",
    "ModelRequestBudgetExhausted",
    "TransportRetryBudgetExhausted",
    "ContextCompactionError",
    "LeaseUnavailableError",
    "ProfileConfigurationError",
    "StateCorruptionError",
    "TerminalPostconditionError",
    "ToolAuthorizationError",
    "ToolConflictError",
}
_SAFE_INTERRUPTION_MESSAGES = {
    "cancelled",
    "paused at a safe boundary",
    "process restarted before episode terminalization",
}


def safe_failure(value: str) -> str:
    """Keep internal diagnostics; never republish opaque Provider/validation inputs.

    The Engine already redacts Profile credentials. External error bodies may also
    contain signed URLs or arbitrary private input, so credential replacement alone
    is insufficient for a persistent, shareable Eval artifact.
    """
    error_type, _, _message = value.partition(":")
    if error_type in _INTERNAL_ERRORS or value in _SAFE_INTERRUPTION_MESSAGES:
        return value
    if re.fullmatch(r"[A-Za-z_]\w*(?:Error|Exception|Exhausted)", error_type):
        return f"{error_type}: [REDACTED] external error details omitted"
    return "[REDACTED] external error details omitted"


def _safe_event(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    safe = dict(payload)
    for field in ("failure", "error", "last_error", "failure_reason"):
        if isinstance(safe.get(field), str):
            value = safe[field]
            if kind == "context_compaction_failed" and field == "error":
                value = f"ContextCompactionError: {value}"
            safe[field] = safe_failure(value)
    if kind in {"run_failed", "run_failure_paused"} and isinstance(safe.get("reason"), str):
        if kind == "run_failed" and safe.get("source") == "reconcile":
            safe["reason"] = f"StateCorruptionError: {safe['reason']}"
        safe["reason"] = safe_failure(safe["reason"])
    return safe


def retain_failure_evidence(
    database_path: Path, project_id: str, report_dir: Path
) -> FailureArtifacts:
    """Retain one consistent SQLite view, redacting only diagnostic error text.

    The live database is opened read-only. SQLite backup includes committed WAL data;
    an in-memory copy plus VACUUM INTO ensures replaced error text is not left in free
    pages of the retained file. Authoritative content and Tool hashes are unchanged.
    """
    stem = f"failed-{project_id}-{uuid.uuid4().hex}"
    artifacts: FailureArtifacts = {
        "database": f"{stem}.sqlite3",
        "diagnostics": f"{stem}.evidence.json",
        "report_json": f"{stem}.report.json",
        "report_markdown": f"{stem}.report.md",
        "manuscript": f"{stem}.manuscript.md",
        "first_failure": None,
    }
    with (
        closing(sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True)) as source,
        closing(sqlite3.connect(":memory:")) as snapshot,
    ):
        source.backup(snapshot)
        snapshot.row_factory = sqlite3.Row
        for table, key, field in (
            ("run_state", "project_id", "failure_reason"),
            ("worker_episodes", "id", "failure"),
        ):
            for row in snapshot.execute(
                f"SELECT {key},{field} FROM {table} WHERE {field} IS NOT NULL"
            ).fetchall():
                snapshot.execute(
                    f"UPDATE {table} SET {field}=? WHERE {key}=?",
                    (safe_failure(row[field]), row[key]),
                )
        events = []
        for row in snapshot.execute(
            "SELECT seq,kind,payload_json,created_at FROM domain_events "
            "WHERE project_id=? ORDER BY seq",
            (project_id,),
        ).fetchall():
            payload = _safe_event(row["kind"], json.loads(row["payload_json"]))
            snapshot.execute(
                "UPDATE domain_events SET payload_json=? WHERE seq=?",
                (json.dumps(payload, ensure_ascii=False), row["seq"]),
            )
            events.append(
                {
                    "seq": row["seq"],
                    "kind": row["kind"],
                    "payload": payload,
                    "created_at": row["created_at"],
                }
            )
        failed_requests = [
            dict(row)
            for row in snapshot.execute(
                "SELECT id,episode_id,request_index,profile_fingerprint,purpose,error_type,"
                "retry_reason,retry_after_ms,backoff_ms,created_at FROM model_requests "
                "WHERE project_id=? AND status='failed' ORDER BY id",
                (project_id,),
            )
        ]
        failure_candidates = [
            {"source": "domain_events", **event}
            for event in events
            if event["kind"].endswith("_failed") or event["kind"] == "run_failure_paused"
        ]
        failure_candidates.extend(
            {
                "source": "model_requests",
                "id": row["id"],
                "kind": "model_request_failed",
                "created_at": row["created_at"],
                "payload": row,
            }
            for row in failed_requests
        )
        first_failure = min(failure_candidates, key=lambda item: item["created_at"], default=None)
        artifacts["first_failure"] = first_failure
        episode_failures = [
            dict(row)
            for row in snapshot.execute(
                "SELECT id,worker,instruction_key,instruction_kind,logical_target,status,"
                "started_at,ended_at,failure FROM worker_episodes WHERE project_id=? "
                "AND status IN ('failed','interrupted') ORDER BY started_at,rowid",
                (project_id,),
            )
        ]
        chains: dict[str, list[dict[str, Any]]] = {}
        for table, order in (("checkpoints", "created_at,rowid"), ("tool_invocations", "id")):
            chains[table] = []
            for row in snapshot.execute(
                f"SELECT * FROM {table} WHERE project_id=? ORDER BY {order}", (project_id,)
            ):
                entry = dict(row)
                entry["result"] = json.loads(entry.pop("result_json"))
                entry["result_hash"] = content_hash(entry["result"])
                chains[table].append(entry)
        snapshot.commit()
        snapshot.execute("VACUUM INTO ?", (str(report_dir / artifacts["database"]),))
    with (report_dir / artifacts["database"]).open("rb") as handle:
        database_hash = file_digest(handle, "sha256").hexdigest()
    diagnostics = {
        "schema_version": 1,
        "project_id": project_id,
        "created_at": datetime.now(UTC).isoformat(),
        "database": artifacts["database"],
        "database_sha256": database_hash,
        "report_json": artifacts["report_json"],
        "redaction": "opaque external error text omitted; authoritative facts and hashes unchanged",
        "first_failure": first_failure,
        "episode_failures": episode_failures,
        "failed_model_requests": failed_requests,
        "events": events,
        **chains,
    }
    (report_dir / artifacts["diagnostics"]).write_text(
        json.dumps(diagnostics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return artifacts
