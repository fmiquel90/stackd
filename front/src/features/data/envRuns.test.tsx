import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { runs, type RunPage } from "@/api/resources";
import type { RunSummary } from "@/api/types";
import { RunsHistory } from "@/features/environments/RunsHistory";
import { failureReason } from "@/features/overview/attention";
import { makeEnv, makeRun } from "@/lib/__fixtures__/factories";
import {
  RECENT_RUNS,
  fetchRecentRuns,
  recentRunsKey,
  useEnvSummaries,
  usePollInterval,
  useRunHistory,
} from "./envRuns";

const ws = vi.hoisted(() => ({
  status: "live" as string,
  handler: null as null | ((s: Record<string, unknown>) => void),
}));
vi.mock("@/api/stream", () => ({
  useConnectionStatus: () => ws.status,
  useLiveSignals: (_channels: string[], fn: (s: Record<string, unknown>) => void) => {
    ws.handler = fn;
  },
}));

function light(over: Parameters<typeof makeRun>[0] = {}): RunSummary {
  const { check_results: _c, variable_provenance: _v, ...rest } = makeRun(over);
  return rest;
}
const page = (list: RunSummary[], next: string | null = null): RunPage<RunSummary> => ({ runs: list, next });

function wrapper(qc: QueryClient) {
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
}
const client = () => new QueryClient({ defaultOptions: { queries: { retry: false } } });

afterEach(() => {
  vi.restoreAllMocks();
  ws.status = "live";
  ws.handler = null;
});

describe("run list requests", () => {
  it("asks for the summary view and reads the next cursor from the response header", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(
        new Response("[]", { headers: { "content-type": "application/json", "X-Next-Cursor": "run-42" } }),
      );
    expect(await runs.summaryPage("env-1", 20, "run-7")).toEqual({ runs: [], next: "run-42" });
    expect(fetchSpy.mock.calls[0][0]).toBe(
      "/api/v1/environments/env-1/runs?view=summary&limit=20&before=run-7",
    );
  });

  it("stops at the first page holding a real outcome", async () => {
    const spy = vi
      .spyOn(runs, "summaryPage")
      .mockResolvedValueOnce(
        page([light({ type: "proposed", triggered_by: "schedule", state: "finished" })], "c1"),
      )
      .mockResolvedValueOnce(page([light({ state: "finished", confirmed_at: "x" })], "c2"));
    const list = await fetchRecentRuns("env-1");
    expect(list).toHaveLength(2);
    expect(spy.mock.calls).toEqual([
      ["env-1", RECENT_RUNS],
      ["env-1", 200, "c1"],
    ]);
  });

  it("reads one small page when it already holds the last outcome", async () => {
    const spy = vi.spyOn(runs, "summaryPage").mockResolvedValue(page([light({ state: "failed" })], "c1"));
    await fetchRecentRuns("env-1");
    expect(spy).toHaveBeenCalledTimes(1);
  });
});

describe("usePollInterval", () => {
  it("does not poll while the WebSocket is live, and falls back to a 60 s poll otherwise", () => {
    expect(renderHook(() => usePollInterval()).result.current).toBe(false);
    ws.status = "reconnecting";
    expect(renderHook(() => usePollInterval()).result.current).toBe(60_000);
  });
});

describe("useEnvSummaries", () => {
  const a = makeEnv({ id: "env-a", stack_id: "stack-1" });
  const b = makeEnv({ id: "env-b", stack_id: "stack-1" });

  it("refetches only the env that owns the run of a signal, even a run it has never seen", async () => {
    const spy = vi.spyOn(runs, "summaryPage").mockResolvedValue(page([]));
    const qc = client();
    const { result } = renderHook(() => useEnvSummaries([a, b]), { wrapper: wrapper(qc) });
    await waitFor(() => expect(result.current.every((s) => !s.loading)).toBe(true));
    expect(spy).toHaveBeenCalledTimes(2);

    act(() =>
      ws.handler?.({ kind: "run_event", run_id: "run-new", environment_id: "env-a", to_state: "preparing" }),
    );
    await waitFor(() => expect(spy).toHaveBeenCalledTimes(3));
    expect(spy.mock.calls[2][0]).toBe("env-a");
    expect(qc.getQueryState(recentRunsKey("env-b"))?.isInvalidated).toBe(false);

    act(() =>
      ws.handler?.({ kind: "run_event", run_id: "run-x", environment_id: "env-other", to_state: "queued" }),
    );
    await new Promise((r) => setTimeout(r, 20));
    expect(spy).toHaveBeenCalledTimes(3);
  });

  it("completes the primary run with its checks and provenance, fetched for that run only", async () => {
    const failed = light({ id: "run-f", environment_id: "env-a", state: "failed", finished_at: "x" });
    const older = light({
      id: "run-o",
      environment_id: "env-a",
      state: "failed",
      created_at: "2026-01-01T00:00:00Z",
    });
    vi.spyOn(runs, "summaryPage").mockResolvedValue(page([failed, older]));
    const get = vi.spyOn(runs, "get").mockResolvedValue({
      ...makeRun({ id: "run-f", state: "failed" }),
      check_results: { results: [{ name: "tfsec", status: "fail", stage: "after_plan" }] },
      variable_provenance: { region: "mock" },
    });
    const { result } = renderHook(() => useEnvSummaries([a]), { wrapper: wrapper(client()) });
    await waitFor(() => expect(result.current[0].summary.primary?.check_results).toBeTruthy());
    const primary = result.current[0].summary.primary!;
    expect(get.mock.calls).toEqual([["run-f"]]);
    expect(primary.variable_provenance).toEqual({ region: "mock" });
    expect(failureReason(primary)).toBe("check tfsec failed at after_plan");
  });
});

describe("env run history", () => {
  it("pages back with the cursor of the previous page", async () => {
    const spy = vi
      .spyOn(runs, "page")
      .mockResolvedValueOnce({ runs: [makeRun({ id: "r1" })], next: "r1" })
      .mockResolvedValueOnce({ runs: [makeRun({ id: "r0" })], next: null });
    const { result } = renderHook(() => useRunHistory("env-a"), { wrapper: wrapper(client()) });
    await waitFor(() => expect(result.current.hasNextPage).toBe(true));
    await act(() => result.current.fetchNextPage());
    await waitFor(() => expect(result.current.hasNextPage).toBe(false));
    expect(spy.mock.calls).toEqual([
      ["env-a", 50, null],
      ["env-a", 50, "r1"],
    ]);
  });

  it("does not claim the loaded runs are all of them, and loads older ones on demand", () => {
    const onMore = vi.fn();
    render(
      <MemoryRouter>
        <RunsHistory
          runs={[makeRun({ state: "finished" })]}
          more
          onMore={onMore}
          kind="all"
          onKind={() => {}}
          emailOf={() => null}
        />
      </MemoryRouter>,
    );
    expect(screen.getByText("1+ runs")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Load older runs" }));
    expect(onMore).toHaveBeenCalled();
  });
});
