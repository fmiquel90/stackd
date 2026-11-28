import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("./client", () => ({ getAccessToken: () => "tok" }));

class FakeSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  static all: FakeSocket[] = [];
  readyState = FakeSocket.CONNECTING;
  sent: Record<string, unknown>[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null;
  constructor(readonly url: string) {
    FakeSocket.all.push(this);
  }
  send(data: string) {
    this.sent.push(JSON.parse(data));
  }
  close() {
    this.readyState = FakeSocket.CLOSED;
  }
  open() {
    this.readyState = FakeSocket.OPEN;
    this.onopen?.();
  }
  serverClose(code: number) {
    this.readyState = FakeSocket.CLOSED;
    this.onclose?.({ code });
  }
  emit(signal: object) {
    this.onmessage?.({ data: JSON.stringify(signal) });
  }
}

const last = () => FakeSocket.all[FakeSocket.all.length - 1];
const flushMicrotasks = () => act(async () => {});

let stream: typeof import("./stream");
let qc: QueryClient;

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function live(channels: string[], onSignal = vi.fn()) {
  const hook = renderHook(({ chs }) => stream.useLiveSignals(chs, onSignal), {
    wrapper,
    initialProps: { chs: channels },
  });
  return { onSignal, ...hook };
}

beforeEach(async () => {
  vi.useFakeTimers();
  vi.stubGlobal("WebSocket", FakeSocket);
  FakeSocket.all = [];
  vi.resetModules(); // the hub is module state
  stream = await import("./stream");
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("live signal hub", () => {
  it("routes a signal to the listeners of its channel only", () => {
    const a = live(["run:a"]);
    const b = live(["run:b"]);
    act(() => last().open());
    act(() => last().emit({ sub: "run:a", kind: "log", run_id: "a", phase: "planning", max_seq: 1 }));
    expect(a.onSignal).toHaveBeenCalledTimes(1);
    expect(b.onSignal).not.toHaveBeenCalled();
  });

  it("unsubscribes when the last listener of a channel unmounts", async () => {
    live(["user:u"]);
    const first = live(["run:a"]);
    const second = live(["run:a"]);
    act(() => last().open());
    const sock = last();
    expect(sock.sent.filter((m) => m.sub === "run:a")).toHaveLength(1);

    first.unmount();
    await flushMicrotasks();
    expect(sock.sent).not.toContainEqual({ unsub: "run:a" });

    second.unmount();
    await flushMicrotasks();
    expect(sock.sent).toContainEqual({ unsub: "run:a" });
    expect(sock.sent).not.toContainEqual({ unsub: "user:u" });

    // Wanted again later: subscribed again on the same socket.
    live(["run:a"]);
    expect(sock.sent.filter((m) => m.sub === "run:a")).toHaveLength(2);
  });

  it("does not unsubscribe a channel released and wanted again in the same commit", async () => {
    live(["user:u"]);
    const page = live(["run:a"]);
    act(() => last().open());
    page.rerender({ chs: ["run:a", "run:b"] });
    await flushMicrotasks();
    expect(last().sent).not.toContainEqual({ unsub: "run:a" });
  });

  it("on a reconnect, re-subscribes, invalidates non-streamed queries and resyncs listeners", async () => {
    const { onSignal } = live(["run:a"]);
    qc.setQueryData(["run", "a"], { id: "a" });
    qc.setQueryData(["run-logs", "a"], []);
    qc.getQueryCache().find({ queryKey: ["run-logs", "a"] })!.options.meta = { streamed: true };

    act(() => last().open());
    expect(onSignal).not.toHaveBeenCalled(); // the first connect is not a reconnect
    expect(qc.getQueryState(["run", "a"])?.isInvalidated).toBe(false);

    act(() => last().serverClose(1006));
    act(() => vi.advanceTimersByTime(2_000));
    expect(FakeSocket.all).toHaveLength(2);
    act(() => last().open());

    expect(last().sent).toContainEqual({ sub: "run:a" });
    expect(onSignal).toHaveBeenCalledWith({ kind: "resync" });
    expect(qc.getQueryState(["run", "a"])?.isInvalidated).toBe(true);
    expect(qc.getQueryState(["run-logs", "a"])?.isInvalidated).toBe(false);
  });

  it("reconnects promptly after a 1012 resync close, whatever the backoff so far", () => {
    live(["run:a"]);
    for (let i = 0; i < 4; i++) {
      act(() => last().serverClose(1006));
      act(() => vi.advanceTimersByTime(30_000));
    }
    expect(FakeSocket.all).toHaveLength(5);
    act(() => last().open());
    act(() => last().serverClose(1012));
    act(() => vi.advanceTimersByTime(1_500));
    expect(FakeSocket.all).toHaveLength(6);
  });

  it("reconnects promptly after a 1008, but backs off when it repeats", () => {
    live(["run:a"]);
    act(() => last().open());
    act(() => last().serverClose(1008));
    act(() => vi.advanceTimersByTime(1_500));
    expect(FakeSocket.all).toHaveLength(2);
    act(() => last().open());
    act(() => last().serverClose(1008));
    act(() => vi.advanceTimersByTime(1_500));
    expect(FakeSocket.all).toHaveLength(2);
    act(() => vi.advanceTimersByTime(1_000));
    expect(FakeSocket.all).toHaveLength(3);
  });

  it("invalidates entity-stream keys on that entity's signals only", () => {
    renderHook(() => stream.useEntityStream("user:u", [["notifications"]]), { wrapper });
    qc.setQueryData(["notifications"], []);
    act(() => last().open());
    act(() => last().emit({ sub: "run:x", kind: "run_event", run_id: "x", to_state: "planning" }));
    expect(qc.getQueryState(["notifications"])?.isInvalidated).toBe(false);
    act(() => last().emit({ sub: "user:u", kind: "notification", user_id: "u" }));
    expect(qc.getQueryState(["notifications"])?.isInvalidated).toBe(true);
  });
});
