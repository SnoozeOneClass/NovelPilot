import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ArtifactSummary } from "../../types/domain";
import { ChapterBrowser } from "./ChapterBrowser";

function summary(path: string, committed: boolean): ArtifactSummary {
  return { path, kind: committed ? "final" : "draft", title: path, status: committed ? "committed" : "candidate", detail: path, candidate: !committed, committed, routing_decision: null, signals: [], event_status: "recorded", event_note: null, profile_id: null, model_snapshot: null };
}

describe("ChapterBrowser", () => {
  it("keeps a historical chapter selected while another chapter is active", async () => {
    const user = userEvent.setup();
    const summaries = [summary("chapters/chapter-001/final.md", true), summary("chapters/chapter-002/draft.md", false)];
    render(<ChapterBrowser activeChapterId="chapter-002" artifactPaths={summaries.map((item) => item.path)} summaries={summaries} onSelectArtifact={vi.fn()} />);

    await user.click(screen.getByRole("button", { name: /第 01 章/ }));

    expect(screen.getByRole("heading", { name: "第 01 章" })).toBeInTheDocument();
    expect(screen.getByText("已提交正史")).toBeInTheDocument();
  });
});
