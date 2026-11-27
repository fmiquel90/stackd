import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Signal } from "@/api/stream";
import type { LogChunk } from "@/api/types";
import { PHASE_ORDER } from "@/lib/logModel";
import { useRunPage } from "./useRunPage";

const mocks = vi.hoisted(() => ({
  signal: null as ((s: Signal) => void) | null,
  logs: vi.fn(),
  logsAfter: vi.fn(),
}));

vi.mock("@/api/stream", () => ({
  useLiveSignals: (_channels: string[], onSignal: (s: Signal) => void) => {
    mocks.signal = onSignal;
  },
}));
vi.mock("@/auth/session", () => ({ useSession: () => ({ data: null }) }));
vi.mock("@/api/resources", () => {
  const never = () => new Promise(() => {});
  return {
    runs: {
      get: () => Promise.resolve({ id: "r1", state: "planning", environment_id: null }),
      logs: mocks.logs,
      logsAfter: mocks.logsAfter,
    },
    environments: { get: never },
    stacks: { get: never, list: never },
    tiers: { list: never },
    users: { mentionable: never },
    comments: { list: never },
    graphApi: { get: never },
    queue: { list: never },
  };
});

function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => (resolve = r));
  return { promise, resolve };
}

const chunk = (phase: string, seq: number): LogChunk => ({
  phase,
  section: null,
  seq,
  lines: [{ t: "00:00:00", msg: `${phase} ${seq}` }],
});
const log = (phase: string, seq: number): Signal => ({ kind: "log", run_id: "r1", phase, max_seq: seq });

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
  return renderHook(() => useRunPage("r1"), { wrapper });
}

beforeEach(() => {
  mocks.signal = null;
  mocks.logs.mockReset().mockResolvedValue([]);
  mocks.logsAfter.mockReset().mockResolvedValue([]);
});

describe("useRunPage log fetching", () => {
  it("fetches again a phase signalled while its fetch was in flight", async () => {
    const { result } = mount();
    await waitFor(() => expect(mocks.logs).toHaveBeenCalled());
    await waitFor(() => expect(mocks.signal).not.toBeNull());
    const first = deferred<LogChunk[]>();
    const second = deferred<LogChunk[]>();
    mocks.logsAfter.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
    await waitFor(() => expect(result.current.logs).toEqual([]));

    act(() => mocks.signal!(log("planning", 0)));
    act(() => mocks.signal!(log("planning", 1)));
    expect(mocks.logsAfter).toHaveBeenCalledTimes(1);

    await act(async () => first.resolve([chunk("planning", 0)]));
    expect(mocks.logsAfter).toHaveBeenCalledTimes(2);
    expect(mocks.logsAfter).toHaveBeenLastCalledWith("r1", "planning", 0);
    await act(async () => second.resolve([chunk("planning", 1)]));
    await waitFor(() => expect(result.current.logs.map((c) => c.seq)).toEqual([0, 1]));
  });

  it("holds a signal received while the whole log loads, then fetches after it", async () => {
    const initial = deferred<LogChunk[]>();
    mocks.logs.mockReturnValueOnce(initial.promise);
    const { result } = mount();
    await waitFor(() => expect(mocks.signal).not.toBeNull());

    act(() => mocks.signal!(log("planning", 3)));
    expect(mocks.logsAfter).not.toHaveBeenCalled();

    mocks.logsAfter.mockResolvedValueOnce([chunk("planning", 3)]);
    await act(async () =>
      initial.resolve([chunk("planning", 0), chunk("planning", 1), chunk("planning", 2)]),
    );
    await waitFor(() => expect(result.current.logs.map((c) => c.seq)).toEqual([0, 1, 2, 3]));
    expect(mocks.logsAfter).toHaveBeenCalledWith("r1", "planning", 2);
  });

  it("fetches every phase after a reconnect and when the run ends", async () => {
    const { result } = mount();
    await waitFor(() => expect(mocks.signal).not.toBeNull());
    await waitFor(() => expect(result.current.logs).toEqual([]));

    act(() => mocks.signal!({ kind: "resync" }));
    await waitFor(() => expect(mocks.logsAfter).toHaveBeenCalledTimes(PHASE_ORDER.length));
    expect(mocks.logsAfter.mock.calls.map((c) => c[1]).sort()).toEqual([...PHASE_ORDER].sort());

    mocks.logsAfter.mockClear();
    act(() => mocks.signal!({ kind: "run_event", run_id: "r1", to_state: "finished" }));
    await waitFor(() => expect(mocks.logsAfter).toHaveBeenCalledTimes(PHASE_ORDER.length));
  });
});
