PRAGMA foreign_keys = ON;

BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS authoring_schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    brief TEXT NOT NULL CHECK(length(trim(brief)) > 0),
    title TEXT,
    target_json TEXT NOT NULL,
    profile_bindings_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS run_state (
    project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK(status IN ('ready','running','paused','failure_paused','cancelled','completed')),
    phase TEXT NOT NULL CHECK(phase IN ('foundation','writing','finalizing','complete')),
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK(cancel_requested IN (0,1)),
    active_instruction_key TEXT,
    active_instruction_kind TEXT,
    active_logical_target TEXT,
    active_fact_version TEXT,
    lock_version INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT,
    lease_expires_at REAL,
    failure_reason TEXT
);

CREATE TABLE IF NOT EXISTS planning_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    audited INTEGER NOT NULL DEFAULT 0 CHECK(audited IN (0,1)),
    planned_through INTEGER NOT NULL CHECK(planned_through >= 0),
    created_at TEXT NOT NULL,
    UNIQUE(project_id, revision)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_authoring_active_plan
ON planning_revisions(project_id) WHERE audited = 1;

CREATE TABLE IF NOT EXISTS content_blobs (
    sha256 TEXT PRIMARY KEY,
    media_type TEXT NOT NULL,
    byte_length INTEGER NOT NULL,
    content BLOB NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chapters (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    chapter_number INTEGER NOT NULL CHECK(chapter_number > 0),
    title TEXT NOT NULL,
    content_sha256 TEXT NOT NULL REFERENCES content_blobs(sha256),
    status TEXT NOT NULL DEFAULT 'committed' CHECK(status = 'committed'),
    commit_instruction_key TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, chapter_number),
    UNIQUE(project_id, commit_instruction_key)
);

CREATE TABLE IF NOT EXISTS chapter_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    chapter_number INTEGER NOT NULL CHECK(chapter_number > 0),
    kind TEXT NOT NULL CHECK(kind IN ('plan','draft','edit','rewrite')),
    content_sha256 TEXT NOT NULL REFERENCES content_blobs(sha256),
    instruction_key TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, chapter_number, kind, version)
);

CREATE TABLE IF NOT EXISTS chapter_facts (
    project_id TEXT NOT NULL,
    chapter_number INTEGER NOT NULL,
    revision INTEGER NOT NULL,
    facts_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, chapter_number, revision),
    FOREIGN KEY(project_id, chapter_number) REFERENCES chapters(project_id, chapter_number) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS canon_snapshots (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    through_chapter INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, version)
);

CREATE TABLE IF NOT EXISTS summaries (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('chapter','arc','volume')),
    boundary INTEGER NOT NULL CHECK(boundary > 0),
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, kind, boundary)
);

CREATE TABLE IF NOT EXISTS reviews (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    boundary INTEGER NOT NULL CHECK(boundary > 0),
    revision INTEGER NOT NULL CHECK(revision > 0),
    verdict TEXT NOT NULL CHECK(verdict IN ('accept','polish','rewrite')),
    dimensions_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, boundary, revision)
);

CREATE TABLE IF NOT EXISTS rewrite_queue (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    chapter_number INTEGER NOT NULL CHECK(chapter_number > 0),
    review_boundary INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','completed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(project_id, chapter_number, review_boundary),
    FOREIGN KEY(project_id, chapter_number) REFERENCES chapters(project_id, chapter_number) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS checkpoints (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    instruction_key TEXT NOT NULL,
    step TEXT NOT NULL,
    logical_target TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, instruction_key, step, logical_target)
);

CREATE TABLE IF NOT EXISTS worker_episodes (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    worker TEXT NOT NULL,
    instruction_key TEXT NOT NULL,
    profile_snapshot_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running','completed','failed','interrupted')),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    failure TEXT
);

CREATE TABLE IF NOT EXISTS tool_invocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    episode_id TEXT NOT NULL REFERENCES worker_episodes(id) ON DELETE CASCADE,
    instruction_key TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    logical_target TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload_hash TEXT NOT NULL,
    result_json TEXT NOT NULL,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    instruction_key TEXT,
    kind TEXT NOT NULL,
    input_json TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    episode_id TEXT NOT NULL REFERENCES worker_episodes(id) ON DELETE CASCADE,
    request_index INTEGER NOT NULL,
    profile_fingerprint TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    cost_microunits INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(episode_id, request_index)
);

CREATE TABLE IF NOT EXISTS domain_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_authoring_events_project_seq ON domain_events(project_id, seq);

INSERT OR IGNORE INTO authoring_schema_migrations(version, applied_at)
VALUES ('0001_initial', CURRENT_TIMESTAMP);

COMMIT;
