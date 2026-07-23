from __future__ import annotations

import gzip
import hashlib
import json
import math
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast

from pydantic import BaseModel
from sqlalchemy import RowMapping, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from app.db.schema import content_blobs, content_refs

CanonicalizerId = Literal["exact-utf8-v1", "canonical-json-v1", "redacted-bytes-v1"]
CompressionId = Literal["identity-v1", "gzip-v1"]
JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


class StorageIntegrityError(RuntimeError):
    """Stored bytes do not match their immutable content identity."""


class ContentReferenceNotFoundError(LookupError):
    """A project-scoped content reference does not exist."""


@dataclass(frozen=True, slots=True)
class PreparedContent:
    canonicalizer_id: CanonicalizerId
    canonical_bytes: bytes
    sha256: str
    compression: CompressionId
    payload: bytes

    @property
    def canonical_size(self) -> int:
        return len(self.canonical_bytes)

    @property
    def stored_size(self) -> int:
        return len(self.payload)


@dataclass(frozen=True, slots=True)
class ContentRefRecord:
    id: str
    project_id: str
    blob_sha256: str
    semantic_kind: str
    media_type: str
    canonicalizer_id: CanonicalizerId
    schema_id: str | None
    schema_version: int | None
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class PackedContent:
    reference: ContentRefRecord
    compression: CompressionId
    canonical_size: int
    stored_size: int
    payload: bytes

    def unpack_and_verify(self) -> bytes:
        if len(self.payload) != self.stored_size:
            raise StorageIntegrityError(
                f"Stored payload length mismatch for content ref {self.reference.id}."
            )
        if self.compression == "identity-v1":
            canonical_bytes = self.payload
        elif self.compression == "gzip-v1":
            try:
                canonical_bytes = gzip.decompress(self.payload)
            except (EOFError, OSError) as exc:
                raise StorageIntegrityError(
                    f"Invalid gzip payload for content ref {self.reference.id}."
                ) from exc
        else:  # pragma: no cover - the database check constraint rejects this first.
            raise StorageIntegrityError(f"Unknown compression codec: {self.compression!r}.")

        if len(canonical_bytes) != self.canonical_size:
            raise StorageIntegrityError(
                f"Canonical length mismatch for content ref {self.reference.id}."
            )
        actual_hash = hashlib.sha256(canonical_bytes).hexdigest()
        if actual_hash != self.reference.blob_sha256:
            raise StorageIntegrityError(
                f"Content hash mismatch for content ref {self.reference.id}."
            )
        return canonical_bytes


def _prepare(canonicalizer_id: CanonicalizerId, canonical_bytes: bytes) -> PreparedContent:
    compressed = gzip.compress(canonical_bytes, compresslevel=9, mtime=0)
    if len(compressed) < len(canonical_bytes):
        compression: CompressionId = "gzip-v1"
        payload = compressed
    else:
        compression = "identity-v1"
        payload = canonical_bytes
    return PreparedContent(
        canonicalizer_id=canonicalizer_id,
        canonical_bytes=canonical_bytes,
        sha256=hashlib.sha256(canonical_bytes).hexdigest(),
        compression=compression,
        payload=payload,
    )


def prepare_exact_text(value: str) -> PreparedContent:
    """Encode application-visible text exactly; whitespace and Unicode are untouched."""
    return _prepare("exact-utf8-v1", value.encode("utf-8"))


def _json_value(value: object) -> JsonValue:
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump(mode="json"))
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical-json-v1 rejects NaN and Infinity.")
        return value
    if isinstance(value, Mapping):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("canonical-json-v1 requires string object keys.")
            result[key] = _json_value(item)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_value(item) for item in value]
    raise TypeError(f"Unsupported canonical JSON value: {type(value).__name__}.")


def prepare_canonical_json(value: object) -> PreparedContent:
    normalized = _json_value(value)
    serialized = json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _prepare("canonical-json-v1", serialized.encode("utf-8"))


def prepare_redacted_bytes(value: bytes) -> PreparedContent:
    """Hash already-redacted diagnostic bytes without further text rewriting."""
    return _prepare("redacted-bytes-v1", bytes(value))


def _content_ref_record(row: RowMapping) -> ContentRefRecord:
    return ContentRefRecord(
        id=cast(str, row["id"]),
        project_id=cast(str, row["project_id"]),
        blob_sha256=cast(str, row["blob_sha256"]),
        semantic_kind=cast(str, row["semantic_kind"]),
        media_type=cast(str, row["media_type"]),
        canonicalizer_id=cast(CanonicalizerId, row["canonicalizer_id"]),
        schema_id=cast(str | None, row["schema_id"]),
        schema_version=cast(int | None, row["schema_version"]),
        created_at_ms=cast(int, row["created_at_ms"]),
    )


class ContentRepository:
    """Project-scoped B1-P content operations bound to one short transaction."""

    def __init__(
        self,
        connection: AsyncConnection,
        *,
        id_factory: Callable[[], str] | None = None,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self._connection = connection
        self._id_factory = id_factory or (lambda: uuid.uuid4().hex)
        self._now_ms = now_ms or (lambda: time.time_ns() // 1_000_000)

    async def put(
        self,
        *,
        project_id: str,
        prepared: PreparedContent,
        semantic_kind: str,
        media_type: str,
        schema_id: str | None = None,
        schema_version: int | None = None,
        ref_id: str | None = None,
        created_at_ms: int | None = None,
    ) -> ContentRefRecord:
        """Insert/reuse a physical Blob and create a new semantic reference atomically."""
        timestamp = self._now_ms() if created_at_ms is None else created_at_ms
        reference_id = self._id_factory() if ref_id is None else ref_id
        insert_blob = sqlite_insert(content_blobs).values(
            project_id=project_id,
            sha256=prepared.sha256,
            compression=prepared.compression,
            canonical_size=prepared.canonical_size,
            stored_size=prepared.stored_size,
            payload=prepared.payload,
            created_at_ms=timestamp,
        )
        await self._connection.execute(
            insert_blob.on_conflict_do_nothing(
                index_elements=[content_blobs.c.project_id, content_blobs.c.sha256]
            )
        )

        existing = (
            await self._connection.execute(
                select(
                    content_blobs.c.compression,
                    content_blobs.c.canonical_size,
                    content_blobs.c.stored_size,
                    content_blobs.c.payload,
                ).where(
                    content_blobs.c.project_id == project_id,
                    content_blobs.c.sha256 == prepared.sha256,
                )
            )
        ).mappings().one()
        existing_reference = ContentRefRecord(
            id=reference_id,
            project_id=project_id,
            blob_sha256=prepared.sha256,
            semantic_kind=semantic_kind,
            media_type=media_type,
            canonicalizer_id=prepared.canonicalizer_id,
            schema_id=schema_id,
            schema_version=schema_version,
            created_at_ms=timestamp,
        )
        existing_packed = PackedContent(
            reference=existing_reference,
            compression=cast(CompressionId, existing["compression"]),
            canonical_size=cast(int, existing["canonical_size"]),
            stored_size=cast(int, existing["stored_size"]),
            payload=cast(bytes, existing["payload"]),
        )
        if existing_packed.unpack_and_verify() != prepared.canonical_bytes:
            raise StorageIntegrityError(
                f"Hash collision or corrupted Blob for project {project_id!r}."
            )

        await self._connection.execute(
            content_refs.insert().values(
                id=reference_id,
                project_id=project_id,
                blob_sha256=prepared.sha256,
                semantic_kind=semantic_kind,
                media_type=media_type,
                canonicalizer_id=prepared.canonicalizer_id,
                schema_id=schema_id,
                schema_version=schema_version,
                created_at_ms=timestamp,
            )
        )
        return existing_reference

    async def get_packed(self, *, project_id: str, ref_id: str) -> PackedContent:
        row = (
            await self._connection.execute(
                select(
                    content_refs,
                    content_blobs.c.compression,
                    content_blobs.c.canonical_size,
                    content_blobs.c.stored_size,
                    content_blobs.c.payload,
                )
                .join(
                    content_blobs,
                    (content_blobs.c.project_id == content_refs.c.project_id)
                    & (content_blobs.c.sha256 == content_refs.c.blob_sha256),
                )
                .where(
                    content_refs.c.project_id == project_id,
                    content_refs.c.id == ref_id,
                )
            )
        ).mappings().one_or_none()
        if row is None:
            raise ContentReferenceNotFoundError(
                f"Content reference {ref_id!r} does not exist in project {project_id!r}."
            )
        return PackedContent(
            reference=_content_ref_record(row),
            compression=cast(CompressionId, row["compression"]),
            canonical_size=cast(int, row["canonical_size"]),
            stored_size=cast(int, row["stored_size"]),
            payload=cast(bytes, row["payload"]),
        )

    async def verify_project(self, project_id: str) -> int:
        rows = (
            await self._connection.execute(
                select(
                    content_refs,
                    content_blobs.c.compression,
                    content_blobs.c.canonical_size,
                    content_blobs.c.stored_size,
                    content_blobs.c.payload,
                )
                .join(
                    content_blobs,
                    (content_blobs.c.project_id == content_refs.c.project_id)
                    & (content_blobs.c.sha256 == content_refs.c.blob_sha256),
                )
                .where(content_refs.c.project_id == project_id)
            )
        ).mappings()
        count = 0
        for row in rows:
            PackedContent(
                reference=_content_ref_record(row),
                compression=cast(CompressionId, row["compression"]),
                canonical_size=cast(int, row["canonical_size"]),
                stored_size=cast(int, row["stored_size"]),
                payload=cast(bytes, row["payload"]),
            ).unpack_and_verify()
            count += 1
        return count
