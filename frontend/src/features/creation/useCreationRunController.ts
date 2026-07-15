import { useEffect, useRef } from "react";
import type { ProjectReadiness } from "../../types/domain";

interface CreationRunControllerOptions {
  hasStarted: boolean;
  readiness: ProjectReadiness | null;
  busy: boolean;
  continuationKey: string;
  onResume: () => Promise<void>;
}

export function useCreationRunController({
  hasStarted,
  readiness,
  busy,
  continuationKey,
  onResume
}: CreationRunControllerOptions): void {
  const attemptedKeys = useRef(new Set<string>());
  const running = useRef(false);
  const resumeRef = useRef(onResume);
  resumeRef.current = onResume;

  useEffect(() => {
    const nextAction = readiness?.next_action;
    if (
      !hasStarted ||
      busy ||
      running.current ||
      nextAction?.id !== "resume_run" ||
      nextAction.requires_user ||
      !nextAction.can_auto_continue
    ) {
      return;
    }
    const key = `${continuationKey}:${nextAction.id}`;
    if (attemptedKeys.current.has(key)) return;
    attemptedKeys.current.add(key);
    running.current = true;
    void resumeRef.current().finally(() => {
      running.current = false;
    });
  }, [busy, continuationKey, hasStarted, readiness]);
}
