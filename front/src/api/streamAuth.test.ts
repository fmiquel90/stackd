import { createElement, type ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { setAccessToken } from "./client";
import { useConnectionStatus, useLiveSignals } from "./stream";

class FakeSocket {
  static OPEN = 1;
  static all: FakeSocket[] = [];
  readyState = 0;
  sent: unknown[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: ((e: { code: number }) => void) | null = null;
  constructor(public url: string) {
    FakeSocket.all.push(this);
  }
  send(data: string) {
    this.sent.push(JSON.parse(data));
  }
  close() {
    this.closeWith(1000);
  }
  open() {
    this.readyState = FakeSocket.OPEN;
    this.onopen?.();
  }
  closeWith(code: number) {
    this.readyState = 3;
    this.onclose?.({ code });
  }
}

// useLiveSignals hands the query client to the hub (a reconnect invalidates queries).
const wrapper = ({ children }: { children: ReactNode }) =>
  createElement(QueryClientProvider, { client: new QueryClient() }, children);

afterEach(() => {
  setAccessToken(null);
  vi.unstubAllGlobals();
});

describe("WebSocket auth (SPECS §5.3)", () => {
  it("answers a 4401 with a token refresh and reconnects at once with the new token", async () => {
    vi.stubGlobal("WebSocket", FakeSocket);
    const refresh = vi.fn(
      async (_url: string) =>
        new Response(JSON.stringify({ access_token: "fresh", user: {} }), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
    );
    vi.stubGlobal("fetch", refresh);
    setAccessToken("stale");

    const { unmount } = renderHook(
      () => {
        useLiveSignals(["run:r1"], () => undefined);
        return useConnectionStatus();
      },
      { wrapper },
    );
    expect(FakeSocket.all).toHaveLength(1);
    act(() => FakeSocket.all[0].open());
    expect(FakeSocket.all[0].sent[0]).toEqual({ type: "auth", token: "stale" });

    await act(async () => {
      FakeSocket.all[0].closeWith(4401);
      await vi.waitFor(() => expect(FakeSocket.all).toHaveLength(2));
    });
    expect(refresh).toHaveBeenCalledTimes(1);
    expect(refresh.mock.calls[0][0]).toBe("/api/v1/auth/refresh");
    act(() => FakeSocket.all[1].open());
    expect(FakeSocket.all[1].sent).toEqual([{ type: "auth", token: "fresh" }, { sub: "run:r1" }]);
    unmount();
  });

  it("stops reconnecting once the refresh is refused (session lost)", async () => {
    FakeSocket.all = [];
    vi.stubGlobal("WebSocket", FakeSocket);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(null, { status: 401 })),
    );
    setAccessToken("stale");

    const { result, unmount } = renderHook(
      () => {
        useLiveSignals(["run:r2"], () => undefined);
        return useConnectionStatus();
      },
      { wrapper },
    );
    act(() => FakeSocket.all[0].open());
    await act(async () => {
      FakeSocket.all[0].closeWith(4401);
      await vi.waitFor(() => expect(result.current).toBe("idle"));
    });
    expect(FakeSocket.all).toHaveLength(1);
    unmount();
  });
});
