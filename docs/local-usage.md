# Local Usage

## Install

From the repository root:

```powershell
python -m pip install -e .[dev]
npm.cmd --prefix frontend install
```

## Run The App

Start backend and frontend in separate terminals:

```powershell
npm.cmd run backend:dev
```

```powershell
npm.cmd run frontend:dev
```

Open:

```text
http://127.0.0.1:5173
```

The frontend proxies API calls to the FastAPI backend at `http://127.0.0.1:8000`.

## Configure An LLM Profile

Use the LLM Profiles panel in the app, or configure one from PowerShell:

```powershell
$env:NOVELPILOT_API_KEY = "<your-api-key>"
npm.cmd run profile:upsert -- --id main --name "Main Provider" --protocol openai-compatible --base-url "https://api.example.com/v1" --model "model-name" --api-key-env NOVELPILOT_API_KEY --select
```

Supported protocols:

- `openai-compatible`
- `anthropic-compatible`

Profiles are stored in `config/llm-profiles.local.json`, which is ignored by git. Generated novel
projects store only sanitized profile/model snapshots.

Test a saved profile:

```powershell
npm.cmd run profile:test -- --profile-id main
```

## Create And Write A Novel

1. Create or open a project from the project selector.
2. Choose `full_auto` or `participatory`.
3. Configure and select an LLM profile.
4. Complete the book setup conversation.
5. Approve the book setup.
6. Start or resume the harness.
7. Watch loop state, visible model output, artifacts, context snapshots, reviews, verification
   signals, patch status, and routing decisions in the three-column workspace.
8. Submit feedback whenever needed. It will be processed at the next safe checkpoint.
9. Export the manuscript when desired.

Export writes:

```text
output/<novel-name>/exports/manuscript.md
```

Only committed chapter `final.md` files are included.

## Local Project Data

Generated projects are stored under:

```text
output/<novel-name>/
```

This directory is ignored by git. It may contain drafts, final chapters, reviews, state patches,
events, exports, and smoke reports.

## Quality Gate

Run the full local quality gate before publishing changes:

```powershell
npm.cmd run typecheck
npm.cmd run lint
npm.cmd run test
npm.cmd --prefix frontend run build
npm.cmd run acceptance
npm.cmd run audit:secrets
```

The fixture-based tests cover project storage, profile safety, LLM adapters, event replay, run
control, feedback routing, artifact summaries, chapter verification, state patch commit/rejection,
retry preparation, and manuscript export.

## Real Provider Smoke And Literary Review

When a real LLM profile is available, run:

```powershell
npm.cmd run smoke:live -- --profile-id main
```

This creates a timestamped smoke project under `output/`, completes setup, runs one bounded
full-auto chapter loop, exports a manuscript, and writes:

```text
exports/live_smoke_report.json
```

After inspecting the generated `final.md`, `review.md`, `verification.json`, and state patch files,
record the human review:

```powershell
npm.cmd run review:literary -- --project "<smoke-project-path>" --decision approved --chapter-assessment "<notes>" --state-patch-assessment "<notes>"
```

Then audit completion:

```powershell
npm.cmd run audit:completion -- --project "<smoke-project-path>"
```

Completion passes only when static acceptance, output secret audit, live provider smoke, and
literary review evidence all pass.

## What Should Stay Local

These paths are intentionally not part of a public push:

```text
config/*.local.json
output/
node_modules/
.tmp/
cache directories
```

If local Trellis or agent-workspace files are present, keep them on a private local branch unless
you intentionally want to publish that workflow history.

