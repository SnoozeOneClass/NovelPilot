# Novelpilot

Novelpilot is a local, single-user writing workbench for long-form AI novel generation.
It is built around a three-layer Agent Loop Harness:

- Book loop: approved long-term novel direction and constraints.
- Story arc loop: rolling current-arc planning, with optional human review.
- Chapter loop: controlled context, draft, candidate observations, semantic review,
  verification, final commit, candidate state patch, and harness-validated canon commit.

The project is intentionally local-first. Novel data is stored as documents and JSON files under
`output/<novel-name>/`; LLM secrets live only in a gitignored local config file.

## Stack

- Backend: FastAPI, Pydantic, local filesystem storage.
- Frontend: React, Vite, TypeScript.
- LLM protocols: OpenAI-compatible and Anthropic-compatible profiles.

## Docs

For the compact public design notes, see:

- [Architecture](docs/architecture.md)
- [Local Usage](docs/local-usage.md)

## Setup

Install Python and frontend dependencies:

```powershell
python -m pip install -e .[dev]
npm.cmd --prefix frontend install
```

Run the backend and frontend in separate terminals:

```powershell
npm.cmd run backend:dev
```

```powershell
npm.cmd run frontend:dev
```

Open the app at:

```text
http://127.0.0.1:5173
```

The frontend proxies API calls to the backend at `http://127.0.0.1:8000`.

## LLM Profiles

Use the LLM Profiles panel in the app to add one or more profiles. A profile contains:

- `id`
- `name`
- `protocol`: `openai-compatible` or `anthropic-compatible`
- `base_url`
- `api_key`
- `model`
- `enabled`

Profiles are stored in:

```text
config/llm-profiles.local.json
```

This file is ignored by git. Novel output folders store only sanitized profile/model snapshots,
never API keys.

After saving a profile, use the test button in the profile row to run a small explicit provider
smoke test before starting the harness.

You can also create or update a profile from PowerShell without placing the API key in command
history:

```powershell
$env:NOVELPILOT_API_KEY = "<your-api-key>"
npm.cmd run profile:upsert -- --id main --name "Main Provider" --protocol openai-compatible --base-url "https://api.example.com/v1" --model "model-name" --api-key-env NOVELPILOT_API_KEY --select
```

For an existing profile, omit `--api-key-env` to preserve the stored key while changing non-secret
fields such as `--model` or `--base-url`.

Test the saved profile from the CLI before running the full harness smoke:

```powershell
npm.cmd run profile:test -- --profile-id main
```

Omit `--profile-id` to test the active profile. The command reports provider/model snapshots and
redacts the selected profile's key/base URL from success or error output. The same redaction is
applied to profile-test API errors, live-smoke diagnostics, and durable `run_failed` events.

Scan generated novel projects for configured profile keys or base URLs before sharing output:

```powershell
npm.cmd run audit:secrets
```

The audit reports only file paths, profile ids, and value kinds. It does not print raw API keys or
base URLs.

## Live Provider Smoke

After configuring a real profile, run a full local harness smoke test:

```powershell
npm.cmd run smoke:live -- --profile-id <profile-id>
```

The command creates a timestamped `output/Novelpilot Live Smoke .../` project, selects the profile,
answers the setup conversation, runs one full-auto chapter loop, exports `exports/manuscript.md`, and
writes `exports/live_smoke_report.json`. It restores the previously active project/profile unless
`--keep-active` is passed.

The setup conversation may include LLM-generated follow-up questions before approval. The smoke
command answers them in a bounded loop so a provider cannot create an unbounded setup interview.

Successful output lists the generated `final.md`, `review.md`, `verification.json`, and state patch
files for manual literary/usefulness review.

After inspecting those files, record the review:

```powershell
npm.cmd run review:literary -- --project "<smoke-project-path>" --decision approved --chapter-assessment "<notes>" --state-patch-assessment "<notes>"
```

You can also open the smoke project in the app, record the review from the Literary Review card,
and inspect completion gates in the right Harness panel.

Then audit completion:

```powershell
npm.cmd run audit:completion -- --project "<smoke-project-path>"
```

Completion audit also scans the audited output path for configured profile API keys and base URLs.
It reports only profile id, value kind, and relative file path if a leak is found.

## Workflow

1. Create or open a novel project.
2. Choose `full_auto` or `participatory` mode.
3. Configure and select an LLM profile.
4. Complete the Book Setup conversation.
5. Approve the book loop.
6. Start or resume the harness.
   Full-auto advancement continues across chapter-complete checkpoints until a human gate, failure,
   cooperative pause, or bounded step budget stops it at a safe checkpoint.
   If the local backend restarts while metadata still says `running` or `pause_requested`, use stale
   run recovery to move the project back to `paused`, then resume from committed state.
7. Watch loop state, model-visible output, artifacts, routing, reviews, verification signals,
   and state patch results in the three-column workspace.
   The Harness panel also shows run readiness gates for setup, active LLM profile, run control,
   and completion evidence.
8. Submit feedback at any time. Feedback is recorded immediately and processed at the next safe
   checkpoint after the current LLM atomic action finishes.
9. If verification or state patch validation fails, use retry current chapter. Novelpilot archives
   the failed candidate artifacts under `attempts/` before regenerating.
10. Export the manuscript when desired. Export uses committed `final.md` chapters only.

## Storage Model

Each novel project is stored under `output/<novel-name>/`:

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
```

`events.jsonl` is the durable harness audit stream. Newly appended events include a project-local
`seq` number, while older no-sequence events remain readable.

Candidate files such as `draft.md`, `observations.json`, and `candidate_state_patch.json` are not
canon. Canon changes occur only through harness-validated committed state patches.

## Validation

Run the full local quality gate:

```powershell
npm.cmd run typecheck
npm.cmd run lint
npm.cmd run test
npm.cmd --prefix frontend run build
npm.cmd run acceptance
npm.cmd run audit:secrets
```

Current fixture-based tests cover project storage, profile safety, LLM adapters, SSE replay, run
control, feedback routing, artifact summaries, chapter verification, state patch commit/rejection,
and manuscript export. The acceptance report maps implemented behavior back to the planning
requirements and keeps real-provider/literary-review checks as manual gates. The output-secret gate
is automated by `audit:secrets` and included in `audit:completion`.

Run `npm.cmd run smoke:live -- --profile-id <profile-id>` when a real LLM profile is available.
After recording literary review through the CLI or app workspace,
`npm.cmd run audit:completion -- --project "<smoke-project-path>"` should report all completion gates
as passed, including `output_secret_audit`.
