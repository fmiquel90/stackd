import { useEffect, useRef, useSyncExternalStore } from "react";
import { type QueryKey, useQueryClient } from "@tanstack/react-query";
import { getAccessToken } from "./client";

/**
 * One multiplexed WebSocket per tab (DESIGN §6), shared by every component. Components declare the
 * channels they need (`run:<id>`, `environment:<id>`, `user:<id>`); the hub subscribes once per
 * channel and re-subscribes after a reconnect. The server sends light signals without the channel
 * name (`{kind, run_id, …}`, SPECS §5.3) and has no unsubscribe, so every listener receives every
 * signal and filters on its payload. TanStack Query stays the source of truth: listeners
 * *invalidate* queries, except logs, which fetch incrementally with `after_seq`.
 */

export type Signal =
  | { kind: "run_event"; run_id: string; to_state: string }
  | { kind: "log"; run_id: string; phase: string; max_seq: number }
  | { kind: "comment_event"; run_id: string }
  | { kind: "notification"; user_id: string }
  | { kind: string; [k: string]: unknown };

export type ConnectionStatus = "idle" | "connecting" | "live" | "reconnecting";

type Listener = (s: Signal) => void;

const wanted = new Map<string, number>(); // channel → reference count
const sent = new Set<string>(); // channels subscribed on the current socket
const listeners = new Set<Listener>();
const statusListeners = new Set<() => void>();
let status: ConnectionStatus = "idle";
let ws: WebSocket | null = null;
let retry: ReturnType<typeof setTimeout> | undefined;
let healthy: ReturnType<typeof setTimeout> | undefined;
let attempts = 0;

function setStatus(next: ConnectionStatus) {
  if (status === next) return;
  status = next;
  statusListeners.forEach((l) => l());
}

function flushSubs() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  for (const ch of wanted.keys()) {
    if (!sent.has(ch)) {
      ws.send(JSON.stringify({ sub: ch }));
      sent.add(ch);
    }
  }
}

function connect() {
  const token = getAccessToken(); // fresh token on every (re)connect → survives expiry
  if (!token || ws || wanted.size === 0) return;
  setStatus(attempts > 0 ? "reconnecting" : "connecting");
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  // Token sent as the first message, never in the URL (it would leak to proxy logs and Referer).
  const sock = new WebSocket(`${proto}://${window.location.host}/api/v1/ws`);
  ws = sock;
  sock.onopen = () => {
    sock.send(JSON.stringify({ type: "auth", token }));
    sent.clear();
    flushSubs();
    setStatus("live");
    // Only reset the backoff once the socket has *stayed* open (an auth reject closes at once).
    healthy = setTimeout(() => (attempts = 0), 5_000);
  };
  sock.onmessage = (e) => {
    let signal: Signal;
    try {
      signal = JSON.parse(String(e.data));
    } catch {
      return;
    }
    listeners.forEach((l) => l(signal));
  };
  sock.onclose = () => {
    if (healthy) clearTimeout(healthy);
    ws = null;
    sent.clear();
    if (wanted.size === 0) {
      setStatus("idle");
      return;
    }
    attempts += 1;
    setStatus("reconnecting");
    // Exponential backoff capped at 30 s; the query polls are the fallback meanwhile.
    retry = setTimeout(
      () => {
        retry = undefined;
        connect();
      },
      Math.min(30_000, 1_000 * 2 ** Math.min(attempts, 5)),
    );
  };
}

function want(channels: string[]) {
  for (const ch of channels) wanted.set(ch, (wanted.get(ch) ?? 0) + 1);
  if (ws) flushSubs();
  else if (!retry) connect(); // a pending retry will connect with the new channels
}

function release(channels: string[]) {
  for (const ch of channels) {
    const n = (wanted.get(ch) ?? 0) - 1;
    if (n <= 0) wanted.delete(ch);
    else wanted.set(ch, n);
  }
  // The server cannot unsubscribe: an unwanted channel stays on the socket until the next reconnect,
  // and listeners ignore its signals. Close when nothing is wanted any more.
  if (wanted.size === 0 && ws) ws.close();
}

/** Subscribe to channels and receive every signal (filter on the payload). */
export function useLiveSignals(channels: string[], onSignal: Listener): void {
  const handler = useRef(onSignal);
  useEffect(() => {
    handler.current = onSignal;
  });
  const key = [...channels].sort().join(",");
  useEffect(() => {
    const list = key ? key.split(",") : [];
    if (!list.length) return;
    const l: Listener = (s) => handler.current(s);
    listeners.add(l);
    want(list);
    return () => {
      listeners.delete(l);
      release(list);
    };
  }, [key]);
}

/** The connection state for the top-bar indicator and the "Reconnecting…" bar (DESIGN §6). */
export function useConnectionStatus(): ConnectionStatus {
  return useSyncExternalStore(
    (cb) => {
      statusListeners.add(cb);
      return () => statusListeners.delete(cb);
    },
    () => status,
    () => "idle",
  );
}

function concerns(sub: string, s: Signal): boolean {
  if (sub.startsWith("run:")) return "run_id" in s && s.run_id === sub.slice(4);
  if (sub.startsWith("user:")) return s.kind === "notification";
  // environment:<id> signals carry the run, not the env: any run event may concern it.
  if (sub.startsWith("environment:")) return s.kind === "run_event";
  return true;
}

/** Invalidate `keys` on any signal that concerns `sub` (pre-redesign pages). */
export function useEntityStream(sub: string, keys: QueryKey[]): void {
  useEntityStreams(sub ? [sub] : [], keys);
}

export function useEntityStreams(subs: string[], keys: QueryKey[]): void {
  const qc = useQueryClient();
  useLiveSignals(subs, (s) => {
    if (subs.some((sub) => concerns(sub, s))) for (const key of keys) qc.invalidateQueries({ queryKey: key });
  });
}
