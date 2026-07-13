import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ExperimentFixtureStatus } from "../../types/domain";
import { ExperimentLab } from "./ExperimentLab";

const checkpoint = {
  source_project_name: "project-1",
  source_project_id: "project-1",
  source_title: "退潮前的十一分钟",
  active_arc_id: "arc-002",
  completed_arc_ids: ["arc-001"],
  warmup_chapter_ids: ["chapter-001", "chapter-002"],
  recommended_target_chapter_count: 11,
  target_chapter_count: 11,
  checkpoint_fingerprint: "a".repeat(64)
};

describe("ExperimentLab", () => {
  it("warns a full-auto project to switch to participatory mode", async () => {
    const user = userEvent.setup();
    const openSettings = vi.fn();

    render(
      <ExperimentLab
        status={null}
        operationMode="full_auto"
        loading={true}
        busy={false}
        onFreeze={vi.fn()}
        onOpenSettings={openSettings}
      />
    );

    expect(screen.getByText("制作母本必须使用参与模式")).toBeInTheDocument();
    expect(screen.getByText("当前项目是全自动模式，请先切换后再生成实验母本。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "请先切换参与模式" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "前往设置切换" }));
    expect(openSettings).toHaveBeenCalledTimes(1);
  });

  it("keeps the experiment-only lab visible and explains an ineligible checkpoint", () => {
    const status: ExperimentFixtureStatus = {
      eligible: false,
      issues: [{ code: "missing_warmup_arc", message: "至少需要一个已经完成的共享预热故事弧。" }],
      checkpoint: null,
      existing_fixture: null
    };

    render(<ExperimentLab status={status} operationMode="participatory" loading={false} busy={false} onFreeze={vi.fn()} onOpenSettings={vi.fn()} />);

    expect(screen.getByRole("heading", { name: "实验室" })).toBeInTheDocument();
    expect(screen.getByText("至少需要一个已经完成的共享预热故事弧。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /检查点未就绪/ })).toBeDisabled();
  });

  it("confirms before freezing an eligible checkpoint", async () => {
    const user = userEvent.setup();
    const freeze = vi.fn().mockResolvedValue(true);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const status: ExperimentFixtureStatus = {
      eligible: true,
      issues: [],
      checkpoint,
      existing_fixture: null
    };

    render(<ExperimentLab status={status} operationMode="participatory" loading={false} busy={false} onFreeze={freeze} onOpenSettings={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: /冻结为实验母本/ }));

    expect(window.confirm).toHaveBeenCalled();
    expect(freeze).toHaveBeenCalledTimes(1);
    expect(screen.getByText("11 章")).toBeInTheDocument();
  });

  it("shows an existing matching fixture without offering another freeze", () => {
    const status: ExperimentFixtureStatus = {
      eligible: true,
      issues: [],
      checkpoint,
      existing_fixture: {
        fixture_id: "fixture-existing",
        created_at: "2026-07-13T00:00:00Z",
        relative_path: "experiments/fixtures/fixture-existing",
        checkpoint
      }
    };

    render(<ExperimentLab status={status} operationMode="participatory" loading={false} busy={false} onFreeze={vi.fn()} onOpenSettings={vi.fn()} />);

    expect(screen.getByText("fixture-existing")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /母本已冻结/ })).toBeDisabled();
  });
});
