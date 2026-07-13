import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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
  it("lets participatory approval override the recommended chapter count", async () => {
    const user = userEvent.setup();
    const approve = vi.fn().mockResolvedValue(true);

    render(
      <StoryArcsView
        currentArc={currentArc}
        activeChapterId={null}
        artifactPaths={[]}
        summaries={[]}
        approving={false}
        onApprove={approve}
        onRequestRevision={vi.fn().mockResolvedValue(true)}
        onSelectArtifact={vi.fn()}
      />
    );

    expect(screen.getByText("Loop 建议 10 章")).toBeInTheDocument();
    const chapterCount = screen.getByRole("spinbutton", { name: /计划章节数/ });
    await user.clear(chapterCount);
    await user.type(chapterCount, "12");
    await user.click(screen.getByRole("button", { name: /批准并继续/ }));

    expect(approve).toHaveBeenCalledWith(12);
  });
});
