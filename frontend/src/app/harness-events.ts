import { useEffect, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { apiUrl } from "../api/client";
import { isHarnessEvent, type HarnessEvent } from "../types/domain";
import { workspaceQueryKeys } from "./workspace-queries";

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
    const source = new EventSource(apiUrl("/api/runs/events"));
    const handleHarnessEvent = (message: MessageEvent<string>) => {
      let parsed: unknown;
      try {
        parsed = JSON.parse(message.data);
      } catch {
        return;
      }
      if (!isHarnessEvent(parsed) || parsed.project_id !== projectId) return;
      setEvents((current) => mergeHarnessEvent(current, parsed));
      if (parsed.kind !== "llm_output_delta") {
        void queryClient.invalidateQueries({ queryKey: workspaceQueryKeys.project(projectId) });
      }
    };

    source.onmessage = handleHarnessEvent;
    source.addEventListener("harness_event", (event) => handleHarnessEvent(event as MessageEvent<string>));
    source.addEventListener("stream_ready", () => undefined);
    return () => source.close();
  }, [projectId, queryClient]);

  return events;
}
