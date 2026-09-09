BEGIN IMMEDIATE;

ALTER TABLE worker_episodes
ADD COLUMN instruction_kind TEXT NOT NULL DEFAULT '';

ALTER TABLE worker_episodes
ADD COLUMN logical_target TEXT NOT NULL DEFAULT '';

ALTER TABLE worker_episodes
ADD COLUMN fallback_profile_snapshot_json TEXT;

ALTER TABLE worker_episodes
ADD COLUMN compaction_failure_count INTEGER NOT NULL DEFAULT 0
CHECK(compaction_failure_count >= 0);

INSERT INTO authoring_schema_migrations(version, applied_at)
VALUES ('0002_episode_runtime_evidence', CURRENT_TIMESTAMP);

COMMIT;
