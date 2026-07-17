import { describe, expect, it } from "vitest";
import type {
  CurrentArcState,
  ProjectReadiness,
  ProjectSummary
} from "../../types/domain";
import { deriveCreationViewModel } from "./creation-view-model";

const project: ProjectSummary = {
  name: "project-1",
  title: "退潮前的十一分钟",
  path: "output/project-1",
  metadata: {
    schema_version: 1,
    project_id: "project-1",
    title: "退潮前的十一分钟",
    operation_mode: "participatory",
    active_profile_id: "main",
    active_arc_id: null,
    active_chapter_id: null,
    run_status: "idle",
    created_at: "2026-07-15T00:00:00Z",
    updated_at: "2026-07-15T00:00:00Z"
  }
};

function readiness(
  id: ProjectReadiness["next_action"]["id"],
  overrides: Partial<ProjectReadiness["next_action"]> = {}
): ProjectReadiness {
  return {
    status: "pending",
    can_start_run: true,
    gates: [],
    next_action: {
      id,
      command: null,
      requires_user: false,
      can_auto_continue: false,
      message: id,
      evidence: [],
      ...overrides
    }
  };
}

describe("deriveCreationViewModel", () => {
  it("shows exactly one explicit start before the first run", () => {
    const model = deriveCreationViewModel({
      project,
      readiness: readiness("start_run", { requires_user: true }),
      currentArc: null,
      bookRevision: null,
      events: []
    });
    expect(model.stage).toBe("ready_to_start");
    expect(model.primaryAction).toBe("start");
  });

  it("makes story arc review the main task", () => {
    const currentArc: CurrentArcState = {
      arc_id: "arc-001",
      status: "planned",
      plan_path: "arcs/arc-001/plan.md",
      human_review: "awaiting_review",
      approved_at: null,
      recommended_target_chapter_count: 10,
      target_chapter_count: 10,
      completed_chapter_ids: [],
      completed_at: null
    };
    const model = deriveCreationViewModel({
      project: {
        ...project,
        metadata: { ...project.metadata, active_arc_id: "arc-001", run_status: "waiting_for_user" }
      },
      readiness: readiness("approve_story_arc", { requires_user: true }),
      currentArc,
      bookRevision: null,
      events: []
    });
    expect(model.stage).toBe("story_arc_review");
    expect(model.primaryAction).toBe("approve_story_arc");
  });

  it("does not pretend an idle resume hint is active generation", () => {
    const model = deriveCreationViewModel({
      project: {
        ...project,
        metadata: { ...project.metadata, run_status: "idle", active_chapter_id: "chapter-001" }
      },
      readiness: readiness("resume_run", { can_auto_continue: true }),
      currentArc: null,
      bookRevision: null,
      events: []
    });
    expect(model.stage).toBe("completed");
    expect(model.primaryAction).toBeNull();
    expect(model.isRunning).toBe(false);
  });

  it("renders paused state with one explicit resume action", () => {
    const model = deriveCreationViewModel({
      project: {
        ...project,
        metadata: { ...project.metadata, run_status: "paused", active_chapter_id: "chapter-001" }
      },
      readiness: readiness("resume_run", {
        requires_user: true,
        can_auto_continue: false,
        message: "The harness is paused and no generation is active."
      }),
      currentArc: null,
      bookRevision: null,
      events: [
        {
          seq: 1,
          event_id: "event-1",
          timestamp: "2026-07-17T00:00:00Z",
          project_id: "project-1",
          run_id: "run-1",
          kind: "run_started",
          loop_layer: "system",
          atomic_action: null,
          status: "started",
          artifact_path: null,
          routing_decision: null,
          message: "Run started.",
          payload: {}
        }
      ]
    });

    expect(model.stage).toBe("paused");
    expect(model.primaryAction).toBe("resume");
    expect(model.isRunning).toBe(false);
    expect(model.title).toBe("当前没有正在进行的生成");
    expect(model.description).not.toContain("不需要手动点击继续");
  });

  it("keeps exhausted chapter repair as an explicit recovery task", () => {
    const model = deriveCreationViewModel({
      project: {
        ...project,
        metadata: { ...project.metadata, run_status: "waiting_for_user", active_chapter_id: "chapter-001" }
      },
      readiness: readiness("retry_current_chapter", { requires_user: true }),
      currentArc: null,
      bookRevision: null,
      events: []
    });
    expect(model.stage).toBe("chapter_recovery");
    expect(model.primaryAction).toBe("retry_chapter");
  });

  it.each(["retry_provider_connection", "retry_failed_run"] as const)(
    "keeps %s as an explicit failed-step recovery",
    (nextActionId) => {
      const model = deriveCreationViewModel({
        project: {
          ...project,
          metadata: { ...project.metadata, run_status: "failed", active_chapter_id: "chapter-001" }
        },
        readiness: readiness(nextActionId, { requires_user: true }),
        currentArc: null,
        bookRevision: null,
        events: []
      });
      expect(model.stage).toBe("failed");
      expect(model.primaryAction).toBe("retry_failed_run");
    }
  );

  it("shows provider waiting as backend-owned progress without a manual action", () => {
    const model = deriveCreationViewModel({
      project: {
        ...project,
        metadata: {
          ...project.metadata,
          run_status: "waiting_for_provider",
          active_chapter_id: "chapter-002"
        }
      },
      readiness: readiness("wait_for_provider_retry", {
        evidence: ["next_wake_at:2026-07-16T12:00:10Z"]
      }),
      currentArc: null,
      bookRevision: null,
      events: []
    });

    expect(model.stage).toBe("waiting_provider");
    expect(model.primaryAction).toBeNull();
    expect(model.isRunning).toBe(true);
    expect(model.description).toContain("2026-07-16T12:00:10Z");
  });
});
