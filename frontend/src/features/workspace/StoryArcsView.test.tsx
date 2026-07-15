import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { CurrentArcState } from "../../types/domain";
import { StoryArcsView } from "./StoryArcsView";

const currentArc: CurrentArcState = {
  arc_id: "arc-002",
  status: "planned",
  plan_path: "arcs/arc-002/plan.md",
  human_review: "awaiting_review",
  approved_at: null,
  recommended_target_chapter_count: 10,
  target_chapter_count: 10,
  completed_chapter_ids: [],
  completed_at: null
};

describe("StoryArcsView", () => {
  it("keeps story arc review controls out of the browse-only story world", () => {
    render(
      <StoryArcsView
        currentArc={currentArc}
        activeChapterId={null}
        artifactPaths={[]}
        summaries={[]}
        onSelectArtifact={vi.fn()}
      />
    );

    expect(screen.getByText(/故事世界只用于浏览/)).toBeInTheDocument();
    expect(screen.queryByRole("spinbutton")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /批准/ })).not.toBeInTheDocument();
  });
});
