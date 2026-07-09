# Novelpilot Architecture

## Product Goal

Novelpilot is a local, single-user web application for AI-assisted long-form novel writing. It is
not mainly a demo that an LLM can produce prose. The project is an engineering showcase for a
long-running Agent Loop Harness that preserves goals, state boundaries, observation, verification,
correction, and recovery.

The first usable version intentionally keeps cloud concerns out of scope: no accounts, no
multi-user collaboration, no remote sync, and no hosted deployment requirement.

## Runtime Shape

```text
React/Vite frontend
  |
  | HTTP commands: project create/open/close, profile CRUD, setup, run control, feedback, export
  | SSE stream: harness run events and visible model output
  v
FastAPI backend
  |
  +-- Project service: output/<novel-name>/ lifecycle
  +-- LLM profile service: gitignored local config
  +-- LLM gateway: OpenAI-compatible and Anthropic-compatible adapters
  +-- Harness orchestrator: book loop, story arc loop, chapter loop
  +-- Storage service: document artifacts, JSON state, events, retries, exports
```

The backend owns all filesystem writes. The frontend is a workbench for choosing projects,
configuring profiles, completing setup, controlling runs, inspecting harness evidence, submitting
feedback, and exporting committed chapters.

## Three Loop Layers

Novelpilot models long-form writing as three nested loops:

- Book loop: long-term genre promise, reader promise, protagonist direction, world constraints,
  ending tendency, and major user intent.
- Story arc loop: rolling current-arc planning, multi-chapter effect, pacing, conflict progression,
  foreshadowing movement, and arc closure.
- Chapter loop: context assembly, chapter goal, draft, candidate observations, review, verification,
  final prose, candidate state patch, and committed canon update.

The story arc loop is rolling and current-arc-only. The system does not require a full upfront
roadmap for the entire book. After an arc finishes, the next arc is planned from committed state,
approved book direction, prior chapters, verification signals, and pending user feedback.

## Human Participation Modes

Book setup is the primary deep human-in-the-loop phase. It behaves like a planning conversation:
the system asks one decision at a time, offers recommended options, accepts custom answers, and
requires approval before the book loop activates.

At project creation the user chooses one operation mode:

- `full_auto`: story arc plans and chapter loops proceed without story-arc approval by default.
- `participatory`: each story arc plan pauses for human review before chapter writing starts.

Both modes allow feedback at any time. Feedback is recorded immediately, but it does not interrupt
an in-flight LLM call. The harness processes it at the next safe checkpoint and records the routing
result.

## Candidate Versus Committed State

The central safety rule is that LLM output is candidate material by default. It is not canon until
the harness validates and commits it.

Each chapter produces these core artifacts:

```text
context_snapshot.json
goal.md
draft.md
observations.json
review.md
verification.json
candidate_state_patch.json
committed_state_patch.json
final.md
```

Important boundaries:

- `draft.md` is candidate prose.
- `observations.json` is candidate observation data extracted from the draft, not canon state.
- `final.md` is written only after verification passes.
- `candidate_state_patch.json` is proposed by the LLM after final prose exists.
- `committed_state_patch.json` is written only after harness validation.
- Canon files are updated only through committed patches.

This prevents state pollution. For example, if a rejected draft says an important character died,
that event must not enter canon just because it appeared in a candidate observation.

## Canon And Storage

Novel projects live under `output/<novel-name>/`:

```text
project.json
events.jsonl
book/
  settings.md
  outline.md
  state.json
  feedback.md
arcs/
  arc-001/
    plan.md
    revision.md
    state.json
chapters/
  chapter-001/
    attempts/
    context_snapshot.json
    goal.md
    draft.md
    observations.json
    review.md
    verification.json
    final.md
    candidate_state_patch.json
    committed_state_patch.json
canon/
  characters.json
  relationships.json
  world_facts.json
  foreshadowing.json
exports/
  manuscript.md
  live_smoke_report.json
  literary_review.json
```

The full manuscript is an export artifact, not live state. It is generated from committed
`final.md` chapter files only.

## Context Snapshots

`context_snapshot.json` is an audit artifact, not a raw prompt dump. It records which sources were
used, their versions, what was injected directly, what was summarized, what was excluded, and why
the harness assembled context that way.

This is one of the project pillars: the frontend can show how the harness controlled what the model
was allowed to see.

## LLM Profiles And Secret Safety

LLM profiles are global local configuration, not novel project data. They are stored in:

```text
config/llm-profiles.local.json
```

Profiles support:

- `openai-compatible`
- `anthropic-compatible`

Novel output may record sanitized provenance such as `profile_id` and `model_snapshot`, but it must
not store API keys, raw base URLs, request headers, or provider config. Secret audit commands scan
generated output before sharing.

## Events, Recovery, And Run Control

`events.jsonl` is the durable harness audit stream. SSE exposes live events to the frontend.

Run control is cooperative:

- Pause requests do not cancel an in-flight LLM action.
- Pause becomes effective at the next safe checkpoint.
- Resume reads committed state and durable events, not partial stream output.
- Stale run recovery can move abandoned `running` or `pause_requested` metadata to `paused` after a
  local backend restart.

Failed verification or rejected state patches can be retried. Retry preparation archives failed
candidate artifacts under `attempts/` instead of deleting evidence.

## Completion Evidence

Automated checks verify static acceptance, type safety, linting, tests, frontend build, and output
secret safety. Two final gates remain deliberately manual:

- Run the full flow against a real configured LLM profile.
- Review the generated chapter and state patch for literary usefulness.

Those gates produce local evidence under the smoke project's `exports/` directory.

