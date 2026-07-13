import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ArtifactSummary, HarnessEvent } from "../../types/domain";
import { EvidenceCenter } from "./EvidenceCenter";

const events: HarnessEvent[] = Array.from({ length: 240 }, (_, index) => ({
  seq: index + 1,
  event_id: `event-${index}`,
  timestamp: "2026-07-13T01:00:00Z",
  project_id: "project-1",
  run_id: "run-1",
  kind: "atomic_action_started",
  loop_layer: "chapter",
  atomic_action: "draft_chapter",
  status: "started",
  artifact_path: null,
  routing_decision: null,
  message: `事件 ${index}`,
  payload: {}
}));

const summaries: ArtifactSummary[] = Array.from({ length: 240 }, (_, index) => ({
  path: `chapters/chapter-${index}/draft.md`,
  kind: "draft",
  title: `章节 ${index}`,
  status: "candidate",
  detail: "候选正文",
  candidate: true,
  committed: false,
  routing_decision: null,
  signals: [],
  event_status: "recorded",
  event_note: null,
  profile_id: "profile-1",
  model_snapshot: "model-1"
}));

describe("EvidenceCenter", () => {
  it("virtualizes long event and artifact lists", async () => {
    const user = userEvent.setup();
    render(
      <EvidenceCenter
        events={events}
        summaries={summaries}
        artifactPaths={summaries.map((summary) => summary.path)}
        selectedArtifactPath={null}
        activeArtifact={null}
        readiness={null}
        completionAudit={null}
        canPause={false}
        canResume={false}
        canRetry={false}
        busy={false}
        onSelectArtifact={vi.fn()}
        onPause={vi.fn(async () => undefined)}
        onResume={vi.fn(async () => undefined)}
        onRetry={vi.fn(async () => undefined)}
        onRefreshAudit={vi.fn(async () => undefined)}
      />
    );

    const eventList = screen.getByTestId("virtual-event-list");
    expect(eventList.querySelectorAll("button").length).toBeGreaterThan(0);
    expect(eventList.querySelectorAll("button").length).toBeLessThan(events.length);

    await user.click(screen.getByRole("button", { name: "产物" }));
    const artifactList = screen.getByTestId("virtual-artifact-list");
    expect(artifactList.querySelectorAll("button").length).toBeGreaterThan(0);
    expect(artifactList.querySelectorAll("button").length).toBeLessThan(summaries.length);
  });

  it("does not attach a previously selected context snapshot to an unrelated event", () => {
    const unrelated = { ...events[0], event_id: "unrelated", artifact_path: null, message: "没有关联产物" };
    render(
      <EvidenceCenter
        events={[unrelated]}
        summaries={summaries}
        artifactPaths={summaries.map((summary) => summary.path)}
        selectedArtifactPath="chapters/chapter-001/context_snapshot.json"
        activeArtifact={{ path: "chapters/chapter-001/context_snapshot.json", content: '{"sources":["book/direction.md"]}' }}
        readiness={null}
        completionAudit={null}
        canPause={false}
        canResume={false}
        canRetry={false}
        busy={false}
        onSelectArtifact={vi.fn()}
        onPause={vi.fn(async () => undefined)}
        onResume={vi.fn(async () => undefined)}
        onRetry={vi.fn(async () => undefined)}
        onRefreshAudit={vi.fn(async () => undefined)}
      />
    );

    expect(screen.queryByRole("heading", { name: "上下文装配快照" })).not.toBeInTheDocument();
  });
});
