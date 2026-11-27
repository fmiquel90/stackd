import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { LogChunk, Run } from "@/api/types";
import { makeRun } from "@/lib/__fixtures__/factories";
import { mergeChunks } from "@/lib/logModel";
import { parsePlan } from "@/lib/planParse";
import { useRunPage } from "./useRunPage";

// Queries the test does not seed stay pending: only the run and its logs matter here.
vi.mock("@/api/resources", () => {
  const never = () => new Promise<never>(() => {});
  const pending = { get: never, list: never, logs: never, logsAfter: never, mentionable: never };
  return {
    comments: pending,
    environments: pending,
    graphApi: pending,
    queue: pending,
    runs: pending,
    stacks: pending,
    tiers: pending,
    users: pending,
  };
});
vi.mock("@/api/stream", () => ({ useLiveSignals: () => {} }));
vi.mock("@/auth/session", () => ({ useSession: () => ({ data: null }) }));
vi.mock("@/lib/planParse", async (original) => {
  const real = await original<typeof import("@/lib/planParse")>();
  return { ...real, parsePlan: vi.fn(real.parsePlan) };
});

const chunk = (phase: string, seq: number, section: string | null, msgs: string[]): LogChunk => ({
  phase,
  seq,
  section,
  lines: msgs.map((msg) => ({ t: "20:00:00", msg })),
});

describe("useRunPage — plan parsing", () => {
  it("parses the plan once planning is over, not again for each apply chunk", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    );
    const run: Run = makeRun({ id: "r1", state: "planning" });
    qc.setQueryData(["run", "r1"], run);
    let logs = [
      chunk("planning", 0, null, ["Initializing..."]),
      chunk("planning", 1, "plan", ["  # null_resource.a will be created"]),
    ];
    qc.setQueryData(["run-logs", "r1"], logs);
    const parse = vi.mocked(parsePlan);
    parse.mockClear();

    const { result } = renderHook(() => useRunPage("r1"), { wrapper });
    // Still planning: `tofu show` may not have streamed the whole section yet.
    expect(result.current.parsed).toBeNull();
    expect(parse).not.toHaveBeenCalled();

    act(() => qc.setQueryData(["run", "r1"], { ...run, state: "applying" }));
    await waitFor(() => expect(result.current.parsed).not.toBeNull());
    expect(parse).toHaveBeenCalledTimes(1);

    for (let seq = 0; seq < 5; seq++) {
      logs = mergeChunks(logs, [chunk("applying", seq, null, ["null_resource.a: Creating..."])]);
      act(() => qc.setQueryData(["run-logs", "r1"], logs));
    }
    await waitFor(() => expect(result.current.logModel.lines).toHaveLength(7));
    expect(parse).toHaveBeenCalledTimes(1);
  });
});
