import { useEffect, useRef, useSyncExternalStore } from "react";
import { type QueryClient, type QueryKey, useQueryClient } from "@tanstack/react-query";
import { getAccessToken } from "./client";

/**
 * One multiplexed WebSocket per tab (DESIGN §6), shared by every component. Components declare the
 * channels they need (`run:<id>`, `environment:<id>`, `user:<id>`, `rollout:<id>`); the hub
 * subscribes once per channel, unsubscribes when the channel's last listener goes, and re-subscribes
 * after a reconnect. The server tags each light signal with its subscription (`sub`, SPECS §5.3) and
 * the hub hands it to that channel's listeners only. TanStack Query stays the source of truth:
 * listeners *invalidate* queries, except logs, which fetch incrementally with `after_seq`.
 *
 * Signals sent while the socket was down are lost: on every reconnect the hub invalidates the
 * queries — but those marked `meta.streamed`, which their owner catches up itself — and hands every
 * listener a `resync` signal.
 */

export type Signal =
  | { kind: "run_event"; run_id: string; to_state: string }
  | { kind: "log"; run_id: string; phase: string; max_seq: number }
  | { kind: "comment_event"; run_id: string }
  | { kind: "notification"; user_id: string }
  | { kind: "resync" }
  | { kind: string; [k: string]: unknown };

export type ConnectionStatus = "idle" | "connecting" | "live" | "reconnecting";

type Listener = (s: Signal) => void;

const listeners = new Map<string, Set<Listener>>(); // the channels wanted → their listeners
const sent = new Set<string>(); // channels subscribed on the current socket
const statusListeners = new Set<() => void>();
let status: ConnectionStatus = "idle";
let ws: WebSocket | null = null;
let retry: ReturnType<typeof setTimeout> | undefined;
let healthy: ReturnType<typeof setTimeout> | undefined;
let attempts = 0;
let opened = false; // a socket was live since the hub left idle: the next open is a reconnect
let lastClose = 0;
let pruning = false;
let queryClient: QueryClient | null = null;

function setStatus(next: ConnectionStatus) {
  if (status === next) return;
  status = next;
  statusListeners.forEach((l) => l());
}

function flushSubs() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  for (const ch of listeners.keys()) {
    if (!sent.has(ch)) {
      ws.send(JSON.stringify({ sub: ch }));
      sent.add(ch);
    }
  }
}

function resync() {
  void queryClient?.invalidateQueries({ predicate: (q) => !q.meta?.streamed });
  const all = new Set([...listeners.values()].flatMap((set) => [...set]));
  all.forEach((l) => l({ kind: "resync" }));
}

// 1012: the server lost its LISTEN connection and dropped every socket to resync them — come back
// at once, jittered since every tab got it together. 1008: over the subscription cap — the new
// socket carries only what is wanted now; a second one in a row means that is still too many.
function reconnectDelay(code: number): number {
  const prompt = code === 1012 || (code === 1008 && lastClose !== 1008);
  lastClose = code;
  if (prompt) return 500 + Math.random() * 1_000;
  attempts += 1;
  return Math.min(30_000, 1_000 * 2 ** Math.min(attempts, 5));
}

function connect() {
  const token = getAccessToken(); // fresh token on every (re)connect → survives expiry
  if (!token || ws || listeners.size === 0) return;
  setStatus(opened || attempts > 0 ? "reconnecting" : "connecting");
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  // Token sent as the first message, never in the URL (it would leak to proxy logs and Referer).
  const sock = new WebSocket(`${proto}://${window.location.host}/api/v1/ws`);
  ws = sock;
  sock.onopen = () => {
    sock.send(JSON.stringify({ type: "auth", token }));
    sent.clear();
    flushSubs();
    setStatus("live");
    if (opened) resync();
    opened = true;
    // Only reset the backoff once the socket has *stayed* open (an auth reject closes at once).
    healthy = setTimeout(() => (attempts = 0), 5_000);
  };
  sock.onmessage = (e) => {
    let signal: Signal & { sub?: unknown };
    try {
      signal = JSON.parse(String(e.data));
    } catch {
      return;
    }
    if (typeof signal.sub === "string") listeners.get(signal.sub)?.forEach((l) => l(signal));
  };
  sock.onclose = (e) => {
    if (healthy) clearTimeout(healthy);
    ws = null;
    sent.clear();
    if (listeners.size === 0) {
      goIdle();
      return;
    }
    setStatus("reconnecting");
    // The query polls are the fallback meanwhile.
    retry = setTimeout(() => {
      retry = undefined;
      connect();
    }, reconnectDelay(e.code));
  };
}

function goIdle() {
  if (retry) clearTimeout(retry);
  if (healthy) clearTimeout(healthy);
  retry = undefined;
  if (ws) {
    ws.onclose = null;
    ws.close();
    ws = null;
  }
  sent.clear();
  attempts = 0;
  opened = false;
  lastClose = 0;
  setStatus("idle");
}

// Deferred to a microtask: a remount (route change, StrictMode) releases and re-wants a channel in
// one commit, which must not cost an unsub/sub round trip.
function schedulePrune() {
  if (pruning) return;
  pruning = true;
  queueMicrotask(() => {
    pruning = false;
    if (listeners.size === 0) {
      goIdle();
      return;
    }
    for (const ch of sent) {
      if (listeners.has(ch)) continue;
      if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ unsub: ch }));
      sent.delete(ch);
    }
  });
}

function add(channels: string[], l: Listener) {
  for (const ch of channels) {
    let set = listeners.get(ch);
    if (!set) listeners.set(ch, (set = new Set()));
    set.add(l);
  }
  if (ws) flushSubs();
  else if (!retry) connect(); // a pending retry will connect with the new channels
}

function remove(channels: string[], l: Listener) {
  for (const ch of channels) {
    const set = listeners.get(ch);
    set?.delete(l);
    if (set?.size === 0) listeners.delete(ch);
  }
  schedulePrune();
}

/** Subscribe to channels and receive their signals, plus `resync` after a reconnect. */
export function useLiveSignals(channels: string[], onSignal: Listener): void {
  const qc = useQueryClient();
  const handler = useRef(onSignal);
  useEffect(() => {
    handler.current = onSignal;
  });
  useEffect(() => {
    queryClient = qc;
  }, [qc]);
  const key = [...channels].sort().join(",");
  useEffect(() => {
    const list = key ? key.split(",") : [];
    if (!list.length) return;
    const l: Listener = (s) => handler.current(s);
    add(list, l);
    return () => remove(list, l);
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

/** Invalidate `keys` on any signal of `sub` (pre-redesign pages). */
export function useEntityStream(sub: string, keys: QueryKey[]): void {
  useEntityStreams(sub ? [sub] : [], keys);
}

export function useEntityStreams(subs: string[], keys: QueryKey[]): void {
  const qc = useQueryClient();
  useLiveSignals(subs, (s) => {
    // A reconnect already invalidated every query.
    if (s.kind === "resync") return;
    for (const key of keys) void qc.invalidateQueries({ queryKey: key });
  });
}
