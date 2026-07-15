# UX Follow-ups

## Book Direction review lacks visible progress

**Status:** Backlog; recorded only, with no implementation in the current change.

When the user selects `准备审阅`, the application is synthesizing the candidate
Book Direction and running its independent review. The current full-screen busy
overlay only says that the operation is in progress. It also obscures the existing
streamed-character progress indicator, so a slow model request can look stalled.

Future work should replace this passive wait with a visible, truthful progress
surface. It should:

- show real stages such as assembling context, synthesizing the candidate, reviewing
  consistency, and preparing the approval view;
- surface streaming progress or a user-visible result summary when the provider and
  backend expose one;
- keep updating during a long request without inventing a percentage;
- preserve the current failure behavior and make retryable errors clear;
- never expose private chain-of-thought or provider-internal reasoning. Only
  user-facing progress, concise rationale summaries, and final review evidence may
  be displayed.

The existing `llm_stream_progress` events and received-character count are the first
integration point to evaluate. The blocking overlay should not hide whatever
progress surface is chosen.

## Story arc approval breaks the normal creation flow

**Status:** Implemented by `.trellis/tasks/07-15-creation-workflow-ux`.

The former creation workbench split one normal participatory flow across the
workbench and Story World. Approving a story arc still required the user to return
to the workbench and click `恢复`, which made a successful transition look like an
interruption.

The replacement `创作` surface now:

- treats story arc planning, human review, approval, and the transition into chapter
  creation as consecutive stages of the same task;
- never requires the user to return to a previous page and click `恢复` after a normal,
  successful story arc approval;
- removes `恢复` and proactive pause from the normal creation flow; abnormal stale-run
  recovery remains failure handling only;
- keeps one clear primary action at each stage and explains what will happen next;
- makes the current state and required user action visible wherever the user lands;
- rebuilds and renames the workbench as the persistent `创作` surface, while keeping
  `故事世界` browse-only;
- keeps one persistent feedback composer and streams sanitized read-only Chapter prose
  into the central surface during generation;
- avoids adding another confirmation unless a real user decision or irreversible
  action requires it.

Book discussion, Story Arc review, and persistent feedback remain the only creative
human-intervention surfaces. The task's PRD and design retain the original failure
analysis and acceptance contract.
