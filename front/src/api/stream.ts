import { useEffect } from "react";
import { type QueryKey, useQueryClient } from "@tanstack/react-query";
import { getAccessToken } from "./client";

/**
 * Subscribe to a single entity's live channel over the multiplexed WebSocket (DESIGN §6):
 * WS events *invalidate* the given queries (TanStack Query stays the source of truth — the
 * socket never patches the cache). A slow poll on the queries remains the reconnection fallback.
 */
export function useEntityStream(sub: string, keys: QueryKey[]): void {
  useEntityStreams(sub ? [sub] : [], keys);
}

/**
 * Subscribe to many channels over a *single* socket (the worker hub accepts one `{sub}`
 * message per channel, §5.3). Used by the dependency graph so a cascade lights up every
 * node live without opening one WebSocket per node. Any event invalidates `keys` — for the
 * graph that's the partial `["env-runs"]` key, so only the observed node queries refetch.
 */
export function useEntityStreams(subs: string[], keys: QueryKey[]): void {
  const qc = useQueryClient();
  const subsKey = [...subs].sort().join(",");
  // Serialize `keys` into the dep so a caller that changes its invalidation keys (without changing
  // subs) re-subscribes instead of running a stale closure.
  const keysKey = JSON.stringify(keys);
  useEffect(() => {
    if (subs.length === 0 || !getAccessToken()) return;
    let stopped = false;
    let ws: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let healthy: ReturnType<typeof setTimeout> | undefined;
    let attempts = 0;

    const connect = () => {
      const token = getAccessToken(); // fresh token on every (re)connect → survives expiry
      if (stopped || !token) return;
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      // Token sent out-of-band as the first message (never in the URL — it would leak to
      // proxy/LB logs, history, Referer). The server authenticates synchronously on this first
      // message, so subscriptions can follow immediately.
      ws = new WebSocket(`${proto}://${window.location.host}/api/v1/ws`);
      ws.onopen = () => {
        ws?.send(JSON.stringify({ type: "auth", token }));
        for (const sub of subs) ws?.send(JSON.stringify({ sub }));
        // Only reset the backoff once the socket has *stayed* open: an open-then-immediate-close
        // (auth/permission reject) must keep backing off instead of looping every ~2s forever.
        healthy = setTimeout(() => {
          attempts = 0;
        }, 5_000);
      };
      ws.onmessage = () => {
        for (const key of keys) qc.invalidateQueries({ queryKey: key });
      };
      // Dropped socket (e.g. token expiry): reconnect with exponential backoff (capped at 30s) so a
      // server that keeps rejecting can't cause a tight reconnect loop. The query poll is the
      // fallback in the meantime.
      ws.onclose = () => {
        if (healthy) clearTimeout(healthy);
        if (stopped) return;
        attempts += 1;
        const delay = Math.min(30_000, 1_000 * 2 ** Math.min(attempts, 5));
        retry = setTimeout(connect, delay);
      };
    };
    connect();

    return () => {
      stopped = true;
      if (retry) clearTimeout(retry);
      if (healthy) clearTimeout(healthy);
      ws?.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [subsKey, keysKey]);
}
