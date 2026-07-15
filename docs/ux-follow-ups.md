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
