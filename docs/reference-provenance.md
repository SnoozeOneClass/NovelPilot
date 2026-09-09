# Authoring Runtime Provenance

NovelPilot's isolated authoring runtime was designed after studying `ainovel-cli` at commit
`6ed363a9fba51dbf40cc2c9b08dac121727960d9`. The reference project is Apache-2.0 licensed.

The following ideas are architectural references: one serial engine, fact-driven routing,
programmatic role dispatch, role-scoped tools, durable checkpoints, bounded retries, rolling
planning, context compaction, and recovery from persisted facts.

The implementation under `backend/app/authoring/` is an independent Python design using Pydantic
AI, SQLite, FastAPI, and NovelPilot's existing Profile adapters. No Go source file or translated
source fragment from `ainovel-cli` is included. If a future change copies or translates reference
source, that change must carry the Apache-2.0 license, upstream attribution, and a modification
notice before merge.

The refactor branch contains only the Authoring runtime and database. The previous three-layer
NovelPilot implementation remains available from `main`; no old-data migration or compatibility
runtime is included here.

Capability evidence deliberately has no wall-clock TTL. It remains usable only while the Profile
configuration fingerprint and the authoring metadata sidecar fingerprint match. Any configuration,
model id, request option, context window, output limit, or price change produces a new fingerprint.
This prevents a long book from expiring a frozen Profile mid-run while still invalidating changed
configuration before the next episode.

`scripts/upsert_authoring_metadata.py` is a local configuration helper written for NovelPilot. It
derives the sidecar fingerprint through NovelPilot's own Profile contract and contains no upstream
source or credential material.
