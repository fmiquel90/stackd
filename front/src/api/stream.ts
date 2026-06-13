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
  useEffect(() => {
    const token = getAccessToken();
    if (!token || subs.length === 0) return;
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${window.location.host}/api/v1/ws?token=${token}`);
    ws.onopen = () => {
      for (const sub of subs) ws.send(JSON.stringify({ sub }));
    };
    ws.onmessage = () => {
      for (const key of keys) qc.invalidateQueries({ queryKey: key });
    };
    return () => ws.close();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [subsKey]);
}
