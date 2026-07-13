import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import { type ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { HarnessEvent } from "../types/domain";
import { useHarnessEvents } from "./harness-events";

class FakeEventSource {
  static current: FakeEventSource | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  listeners = new Map<string, Array<(event: MessageEvent<string>) => void>>();

  constructor(_url: string) { FakeEventSource.current = this; }
  addEventListener(type: string, listener: EventListener) {
    const callbacks = this.listeners.get(type) ?? [];
    callbacks.push(listener as (event: MessageEvent<string>) => void);
    this.listeners.set(type, callbacks);
  }
  close() { /* no-op */ }
  emit(event: HarnessEvent) {
    const message = new MessageEvent<string>("harness_event", { data: JSON.stringify(event) });
    this.listeners.get("harness_event")?.forEach((listener) => listener(message));
  }
}

const event: HarnessEvent = {
  seq: 1, event_id: "event-1", timestamp: "2026-07-13T00:00:00Z", project_id: "project-1", run_id: "run-1", kind: "run_started", loop_layer: "system", atomic_action: null, status: "started", artifact_path: null, routing_decision: null, message: "started", payload: {}
};

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  FakeEventSource.current = null;
});

describe("useHarnessEvents", () => {
  it("deduplicates replay and batches query invalidation during history catch-up", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("EventSource", FakeEventSource);
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidate = vi.spyOn(client, "invalidateQueries").mockResolvedValue(undefined);
    const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    const { result } = renderHook(() => useHarnessEvents("project-1"), { wrapper });

    act(() => {
      FakeEventSource.current?.emit(event);
      FakeEventSource.current?.emit(event);
      FakeEventSource.current?.emit({ ...event, seq: 2, event_id: "event-2" });
    });

    expect(result.current).toHaveLength(2);
    expect(invalidate).not.toHaveBeenCalled();
    await act(async () => { vi.advanceTimersByTime(100); });
    expect(invalidate).toHaveBeenCalledTimes(2);
  });
});
