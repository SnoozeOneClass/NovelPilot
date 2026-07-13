<!-- TRELLIS:START -->
# Trellis Instructions

These instructions are for AI assistants working in this project.

This project is managed by Trellis. The working knowledge you need lives under `.trellis/`:

- `.trellis/workflow.md` — development phases, when to create tasks, skill routing
- `.trellis/spec/` — package- and layer-scoped coding guidelines (read before writing code in a given layer)
- `.trellis/workspace/` — per-developer journals and session traces
- `.trellis/tasks/` — active and archived tasks (PRDs, research, jsonl context)

If a Trellis command is available on your platform (e.g. `/trellis:finish-work`, `/trellis:continue`), prefer it over manual steps. Not every platform exposes every command.

If you're using Codex or another agent-capable tool, additional project-scoped helpers may live in:
- `.agents/skills/` — reusable Trellis skills
- `.codex/agents/` — optional custom subagents

Managed by Trellis. Edits outside this block are preserved; edits inside may be overwritten by a future `trellis update`.

<!-- TRELLIS:END -->

## Repository Publishing

- The standard way to publish completed work is plain Git: inspect the scoped diff,
  stage the intended files, create a commit, and run `git push` to the configured
  GitHub remote.
- GitHub CLI (`gh`) and pull requests are not prerequisites for a requested push.
  Use them only when the user explicitly asks for a pull request or GitHub-specific
  workflow.
- After the user confirms a task should be published, finish relevant checks and then
  execute `git commit` plus `git push`; do not stop merely because `gh` is unavailable.
- Never stage local secrets or generated data such as `config/*.local.json`, `output/`,
  virtual environments, dependency directories, or ignored Trellis workspace state.
- Report the pushed branch and commit hash after the remote accepts the push.
