import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { apiUrl } from "../api/client";
import { isHarnessEvent, type HarnessEvent } from "../types/domain";
import { workspaceQueryKeys } from "./workspace-queries";

export type WorkspaceRefreshTarget = "project" | "readiness" | "arc" | "artifacts" | "canon" | "completion";

export function refreshTargetsForEvent(event: HarnessEvent): WorkspaceRefreshTarget[] {
  if (event.kind === "llm_output_delta" || event.kind === "llm_stream_progress") return [];
  const targets = new Set<WorkspaceRefreshTarget>(["project", "readiness"]);
  if (event.loop_layer === "story_arc" || event.kind.includes("arc")) targets.add("arc");
  if (event.artifact_path || event.kind.includes("artifact") || event.kind.includes("chapter")) targets.add("artifacts");
  if (event.kind.includes("state_patch") || event.kind.includes("canon") || event.atomic_action === "commit_state_patch") targets.add("canon");
  if (event.kind.includes("literary_review") || event.kind.includes("completion") || event.kind.includes("smoke")) targets.add("completion");
  return [...targets];
}

export function mergeHarnessEvent(current: HarnessEvent[], incoming: HarnessEvent): HarnessEvent[] {
  return current.some((event) => event.event_id === incoming.event_id)
    ? current
    : [...current, incoming];
}

export function useHarnessEvents(projectId: string): HarnessEvent[] {
  const queryClient = useQueryClient();
  const [events, setEvents] = useState<HarnessEvent[]>([]);

  useEffect(() => {
    setEvents([]);
    const eventIds = new Set<string>();
    const pendingTargets = new Set<WorkspaceRefreshTarget>();
    let refreshTimer: number | null = null;
    const source = new EventSource(apiUrl("/api/runs/events"));

    const flushRefreshTargets = () => {
      refreshTimer = null;
      const targets = [...pendingTargets];
      pendingTargets.clear();
      const invalidations = targets.flatMap((target) => {
        switch (target) {
          case "project": return [queryClient.invalidateQueries({ queryKey: workspaceQueryKeys.activeProject(projectId), exact: true })];
          case "readiness": return [queryClient.invalidateQueries({ queryKey: workspaceQueryKeys.readiness(projectId), exact: true })];
          case "arc": return [queryClient.invalidateQueries({ queryKey: workspaceQueryKeys.arc(projectId), exact: true })];
          case "artifacts": return [
            queryClient.invalidateQueries({ queryKey: workspaceQueryKeys.artifactPaths(projectId), exact: true }),
            queryClient.invalidateQueries({ queryKey: workspaceQueryKeys.artifactSummaries(projectId), exact: true })
          ];
          case "canon": return [queryClient.invalidateQueries({ queryKey: workspaceQueryKeys.canonRoot(projectId) })];
          case "completion": return [queryClient.invalidateQueries({ queryKey: workspaceQueryKeys.completion(projectId), exact: true })];
        }
      });
      if (invalidations.length) void Promise.all(invalidations);
    };

    const handleHarnessEvent = (message: MessageEvent<string>) => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(message.data);
      } catch {
        return;
      }
      if (!isHarnessEvent(parsed) || parsed.project_id !== projectId) return;
      if (eventIds.has(parsed.event_id)) return;
      eventIds.add(parsed.event_id);
      setEvents((current) => [...current, parsed]);
      refreshTargetsForEvent(parsed).forEach((target) => pendingTargets.add(target));
      if (pendingTargets.size && refreshTimer === null) refreshTimer = window.setTimeout(flushRefreshTargets, 80);
    };

    source.onmessage = handleHarnessEvent;
    source.addEventListener("harness_event", (event) => handleHarnessEvent(event as MessageEvent<string>));
    source.addEventListener("stream_ready", () => undefined);
    return () => {
      source.close();
      if (refreshTimer !== null) window.clearTimeout(refreshTimer);
    };
  }, [projectId, queryClient]);

  return events;
}
