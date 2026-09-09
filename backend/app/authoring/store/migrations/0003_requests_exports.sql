BEGIN IMMEDIATE;

CREATE TABLE model_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    episode_id TEXT NOT NULL REFERENCES worker_episodes(id) ON DELETE CASCADE,
    request_index INTEGER NOT NULL CHECK(request_index > 0),
    profile_fingerprint TEXT NOT NULL,
    purpose TEXT NOT NULL CHECK(purpose IN ('worker','context_summary','judge')),
    status TEXT NOT NULL CHECK(status IN ('succeeded','failed')),
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    cost_microunits INTEGER NOT NULL DEFAULT 0,
    retry_reason TEXT,
    retry_after_ms INTEGER,
    backoff_ms INTEGER,
    error_type TEXT,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(episode_id, request_index)
);

CREATE TABLE export_manifests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    format TEXT NOT NULL CHECK(format IN ('markdown','txt')),
    source_fingerprint TEXT NOT NULL,
    content_sha256 TEXT NOT NULL REFERENCES content_blobs(sha256),
    byte_length INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, format, source_fingerprint)
);

INSERT INTO authoring_schema_migrations(version, applied_at)
VALUES ('0003_requests_exports', CURRENT_TIMESTAMP);

COMMIT;
