import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { BenchmarkFixtureLifecycle, ExperimentFixtureStatus } from "../../types/domain";
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

const preparing: BenchmarkFixtureLifecycle = {
  status: "preparing",
  fixture_id: null,
  checkpoint_fingerprint: null,
  failure_code: null,
  failure_message: null
};

describe("ExperimentLab", () => {
  it("does not offer conversion or freezing for an ordinary project", () => {
    render(
      <ExperimentLab
        status={null}
        projectKind="novel"
        lifecycle={null}
        loading={false}
        busy={false}
        onRetry={vi.fn()}
      />
    );

    expect(screen.getByRole("heading", { name: "实验室" })).toBeInTheDocument();
    expect(screen.getByText("当前小说不是实验母本项目")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /冻结/ })).not.toBeInTheDocument();
  });

  it("shows normal preparation progress before the checkpoint", () => {
    const status: ExperimentFixtureStatus = {
      project_kind: "benchmark_mother",
      lifecycle: preparing,
      eligible: false,
      issues: [{ code: "missing_warmup_arc", message: "至少需要一个已经完成的共享预热故事弧。" }],
      checkpoint: null,
      existing_fixture: null
    };

    render(<ExperimentLab status={status} projectKind="benchmark_mother" lifecycle={preparing} loading={false} busy={false} onRetry={vi.fn()} />);

    expect(screen.getByText("至少需要一个已经完成的共享预热故事弧。")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "等待自动冻结" })).toBeDisabled();
  });

  it("offers only a local retry after automatic publication fails", async () => {
    const user = userEvent.setup();
    const retry = vi.fn().mockResolvedValue(true);
    const failed: BenchmarkFixtureLifecycle = {
      ...preparing,
      status: "freeze_failed",
      failure_code: "fixture_publication_failed",
      failure_message: "母本文件发布失败，请在实验室重试。"
    };
    const status: ExperimentFixtureStatus = {
      project_kind: "benchmark_mother",
      lifecycle: failed,
      eligible: true,
      issues: [],
      checkpoint,
      existing_fixture: null
    };

    render(<ExperimentLab status={status} projectKind="benchmark_mother" lifecycle={failed} loading={false} busy={false} onRetry={retry} />);
    await user.click(screen.getByRole("button", { name: "重试母本冻结" }));

    expect(retry).toHaveBeenCalledTimes(1);
    expect(screen.getByText(/不会调用模型/)).toBeInTheDocument();
    expect(screen.getByText("11 章")).toBeInTheDocument();
  });

  it("offers local lifecycle reconciliation after a fixture was already published", async () => {
    const user = userEvent.setup();
    const retry = vi.fn().mockResolvedValue(true);
    const status: ExperimentFixtureStatus = {
      project_kind: "benchmark_mother",
      lifecycle: preparing,
      eligible: true,
      issues: [],
      checkpoint,
      existing_fixture: {
        fixture_version: "fixture-v1",
        integrity_verified: true,
        fixture_id: "fixture-existing",
        created_at: "2026-07-13T00:00:00Z",
        relative_path: "experiments/fixtures/fixture-existing",
        checkpoint
      }
    };

    render(<ExperimentLab status={status} projectKind="benchmark_mother" lifecycle={preparing} loading={false} busy={false} onRetry={retry} />);
    await user.click(screen.getByRole("button", { name: "完成母本状态同步" }));

    expect(retry).toHaveBeenCalledTimes(1);
    expect(screen.getByText("检测到已发布母本，等待完成状态同步")).toBeInTheDocument();
  });

  it("shows an existing frozen fixture without another mutation action", () => {
    const frozen: BenchmarkFixtureLifecycle = {
      ...preparing,
      status: "frozen",
      fixture_id: "fixture-existing",
      checkpoint_fingerprint: checkpoint.checkpoint_fingerprint
    };
    const status: ExperimentFixtureStatus = {
      project_kind: "benchmark_mother",
      lifecycle: frozen,
      eligible: true,
      issues: [],
      checkpoint,
      existing_fixture: {
        fixture_version: "fixture-v1",
        integrity_verified: true,
        fixture_id: "fixture-existing",
        created_at: "2026-07-13T00:00:00Z",
        relative_path: "experiments/fixtures/fixture-existing",
        checkpoint
      }
    };

    render(<ExperimentLab status={status} projectKind="benchmark_mother" lifecycle={frozen} loading={false} busy={false} onRetry={vi.fn()} />);

    expect(screen.getByText("fixture-existing")).toBeInTheDocument();
    expect(screen.getByText("fixture-v1 · 完整性已校验")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "母本已冻结" })).toBeDisabled();
  });
});
