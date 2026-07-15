import { renderHook, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ProjectReadiness } from "../../types/domain";
import { useCreationRunController } from "./useCreationRunController";

const autoReadiness: ProjectReadiness = {
  status: "pending",
  can_start_run: true,
  gates: [],
  next_action: {
    id: "resume_run",
    command: "resume",
    requires_user: false,
    can_auto_continue: true,
    message: "continue",
    evidence: []
  }
};

describe("useCreationRunController", () => {
  it("never starts or resumes before the explicit first start", async () => {
    const onResume = vi.fn().mockResolvedValue(undefined);
    renderHook(() => useCreationRunController({
      hasStarted: false,
      readiness: autoReadiness,
      busy: false,
      continuationKey: "v1",
      onResume
    }));
    await new Promise((resolve) => window.setTimeout(resolve, 10));
    expect(onResume).not.toHaveBeenCalled();
  });

  it("continues one authoritative non-user action once", async () => {
    const onResume = vi.fn().mockResolvedValue(undefined);
    const { rerender } = renderHook(
      (props: { key: string; readiness: ProjectReadiness | null }) =>
        useCreationRunController({
          hasStarted: true,
          readiness: props.readiness,
          busy: false,
          continuationKey: props.key,
          onResume
        }),
      { initialProps: { key: "v1", readiness: autoReadiness as ProjectReadiness | null } }
    );
    await waitFor(() => expect(onResume).toHaveBeenCalledTimes(1));
    rerender({ key: "v1", readiness: autoReadiness });
    await new Promise((resolve) => window.setTimeout(resolve, 10));
    expect(onResume).toHaveBeenCalledTimes(1);
    rerender({ key: "v2", readiness: autoReadiness });
    await waitFor(() => expect(onResume).toHaveBeenCalledTimes(2));
  });

  it("stops at every user-required gate", async () => {
    const onResume = vi.fn().mockResolvedValue(undefined);
    renderHook(() => useCreationRunController({
      hasStarted: true,
      readiness: {
        ...autoReadiness,
        next_action: {
          ...autoReadiness.next_action,
          id: "approve_story_arc",
          requires_user: true,
          can_auto_continue: false
        }
      },
      busy: false,
      continuationKey: "v1",
      onResume
    }));
    await new Promise((resolve) => window.setTimeout(resolve, 10));
    expect(onResume).not.toHaveBeenCalled();
  });
});
