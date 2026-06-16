import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { observability, pools, workers } from "@/api/resources";
import type { HealthWorker, LogEntry, PoolCreated } from "@/api/types";
import { useIsAdmin } from "@/auth/session";
import { Button, Card, DeleteButton, Field, PageTitle, Select, TextInput } from "@/components/ui";

function Metric({ label, value, tone }: { label: string; value: string | number; tone?: string }) {
  return (
    <Card>
      <div className="font-data text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
        {label}
      </div>
      <div className="font-data mt-1 text-[22px]" style={{ color: tone ?? "var(--color-text-primary)" }}>
        {value}
      </div>
    </Card>
  );
}

// Raw telemetry: only failures stand out (red). Other levels stay neutral — amber is reserved for
// "needs a human" in the product surfaces, not log severity (DESIGN §3.2).
const LEVEL_COLOR: Record<string, string> = {
  ERROR: "var(--color-state-failed)",
  CRITICAL: "var(--color-state-failed)",
  WARNING: "var(--color-text-primary)",
  INFO: "var(--color-text-secondary)",
  DEBUG: "var(--color-text-secondary)",
};

function statusColor(status: number): string {
  if (status >= 500) return "var(--color-state-failed)";
  if (status >= 200 && status < 300) return "var(--color-state-finished)";
  return "var(--color-text-secondary)";
}

// http.request access logs carry method/path/status/duration — render them instead of the bare
// "request" message so a WARNING is self-explanatory (which route, what status, how slow).
function HttpDetail({ e }: { e: LogEntry }) {
  if (e.method === undefined || e.path === undefined) return null;
  const slow = (e.duration_ms ?? 0) > 1500;
  return (
    <>
      <span style={{ color: "var(--color-text-primary)" }}>
        {e.method} {e.path}
      </span>
      {e.status !== undefined && (
        <span style={{ color: statusColor(e.status) }}> → {e.status}</span>
      )}
      {e.duration_ms !== undefined && (
        <span style={{ color: slow ? "var(--color-text-primary)" : "var(--color-text-secondary)" }}>
          {" "}
          · {e.duration_ms}ms{slow ? " (slow)" : ""}
        </span>
      )}
    </>
  );
}

function DiagnosticsPanel({ workerId, onClose }: { workerId: string; onClose: () => void }) {
  const request = useMutation({ mutationFn: () => workers.requestDiagnostics(workerId) });
  const { data } = useQuery({
    queryKey: ["diagnostics", workerId],
    queryFn: () => workers.diagnostics(workerId),
    refetchInterval: (q) => (q.state.data?.status === "done" || q.state.data?.status === "failed" ? false : 1500),
  });
  // Fire a fresh request when the panel opens (guard against rapid re-fires queueing requests).
  useEffect(() => {
    if (!request.isPending) request.mutate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workerId]);

  const r = (data?.result ?? {}) as Record<string, unknown>;
  const tools = (r.tools ?? {}) as Record<string, string>;
  const disk = (r.disk ?? {}) as Record<string, number>;
  const logs = (r.recent_logs ?? []) as string[];
  const envNames = (r.env_var_names ?? []) as string[];

  return (
    <Card>
      <div className="mb-2 flex items-center justify-between">
        <span className="text-[13px] font-medium">
          Diagnostics · {workerId.slice(0, 8)}{" "}
          <span className="font-data text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
            ({data?.status ?? "requesting…"})
          </span>
        </span>
        <Button onClick={onClose}>Close</Button>
      </div>
      {data?.status === "done" ? (
        <div className="font-data flex flex-col gap-1 text-[12px]">
          <div>platform: {String(r.platform)} · python {String(r.python)} · runner {String(r.runner)}</div>
          <div>tofu: {tools.tofu} · git: {tools.git}</div>
          <div>disk: {disk.free_gb}GB free / {disk.total_gb}GB ({disk.used_pct}% used)</div>
          <div style={{ color: "var(--color-text-secondary)" }}>{envNames.length} env vars (names only — no values)</div>
          <div className="mt-2" style={{ color: "var(--color-text-secondary)" }}>recent agent logs:</div>
          <div className="max-h-[200px] overflow-auto p-2" style={{ backgroundColor: "var(--color-bg-base)", borderRadius: 4 }}>
            {logs.length ? logs.map((l, i) => <div key={i}>{l}</div>) : <span style={{ color: "var(--color-text-secondary)" }}>—</span>}
          </div>
        </div>
      ) : (
        <span className="font-data text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
          Waiting for the worker to run the bundle on its next heartbeat…
        </span>
      )}
    </Card>
  );
}

function LogsPanel({ workerId, setWorkerId }: { workerId: string; setWorkerId: (v: string) => void }) {
  const [level, setLevel] = useState("");
  const [q, setQ] = useState("");
  const params: Record<string, string> = {};
  if (level) params.level = level;
  if (workerId) params.worker_id = workerId;
  if (q) params.q = q;
  const { data } = useQuery({
    queryKey: ["logs", level, workerId, q],
    queryFn: () => observability.logs(params),
    refetchInterval: 3000,
  });

  return (
    <Card>
      <div className="mb-3 flex items-center gap-3">
        <span className="text-[13px] font-medium">Logs</span>
        <Select value={level} onChange={(e) => setLevel(e.target.value)}>
          <option value="">all levels</option>
          <option value="INFO">INFO+</option>
          <option value="WARNING">WARNING+</option>
          <option value="ERROR">ERROR+</option>
        </Select>
        <TextInput placeholder="worker id" value={workerId} onChange={(e) => setWorkerId(e.target.value)} />
        <TextInput placeholder="search" value={q} onChange={(e) => setQ(e.target.value)} />
        <span className="font-data text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
          {data?.total ?? 0} matches
        </span>
      </div>
      <div
        className="font-data max-h-[460px] overflow-auto p-2 text-[12px]"
        style={{ backgroundColor: "var(--color-bg-base)", borderRadius: 4, lineHeight: 1.5 }}
      >
        {(data?.items ?? []).map((e: LogEntry, i) => (
          <div key={`${e.ts}-${e.logger}-${i}`} className="whitespace-pre-wrap">
            <span style={{ color: "var(--color-text-secondary)" }}>{e.ts.slice(11, 19)} </span>
            <span style={{ color: LEVEL_COLOR[e.level] ?? "var(--color-text-secondary)" }}>{e.level.padEnd(5)} </span>
            <span style={{ color: "var(--color-text-secondary)" }}>{e.logger} </span>
            {e.event ? <span style={{ color: "var(--color-accent)" }}>{e.event} </span> : null}
            {/* For http access logs show method/path/status/duration; otherwise the message. */}
            {e.method !== undefined ? <HttpDetail e={e} /> : e.msg}
            {e.run_id ? <span style={{ color: "var(--color-text-secondary)" }}> run={String(e.run_id).slice(0, 8)}</span> : null}
            {e.worker_id ? <span style={{ color: "var(--color-text-secondary)" }}> worker={String(e.worker_id).slice(0, 8)}</span> : null}
          </div>
        ))}
        {data && data.items.length === 0 && (
          <span style={{ color: "var(--color-text-secondary)" }}>No matching logs.</span>
        )}
      </div>
    </Card>
  );
}

function LabelChips({ labels }: { labels: Record<string, unknown> | null }) {
  const entries = Object.entries(labels ?? {});
  if (entries.length === 0) return <span style={{ color: "var(--color-text-secondary)" }}>—</span>;
  return (
    <span className="flex flex-wrap gap-1">
      {entries.map(([k, v]) => (
        <span
          key={k}
          className="rounded-badge px-1.5 text-[11px]"
          style={{ border: "1px solid var(--color-border)", color: "var(--color-text-secondary)" }}
        >
          {k}={String(v)}
        </span>
      ))}
    </span>
  );
}

// Workers are heterogeneous and routed by pool + labels (§7), so group them by pool — preserving
// first-seen order — rather than showing one flat list.
function groupByPool(items: HealthWorker[]): [string | null, HealthWorker[]][] {
  const map = new Map<string | null, HealthWorker[]>();
  for (const w of items) {
    const list = map.get(w.pool) ?? [];
    list.push(w);
    map.set(w.pool, list);
  }
  return [...map.entries()];
}

// Admin-only pool management. Creating a pool returns its agent token once, in cleartext — surfaced
// in a dismissible banner the operator must copy now (it's hashed at rest and never shown again).
function PoolsPanel() {
  const qc = useQueryClient();
  const { data } = useQuery({ queryKey: ["worker-pools"], queryFn: pools.list });
  const [name, setName] = useState("");
  const [created, setCreated] = useState<PoolCreated | null>(null);
  const create = useMutation({
    mutationFn: () => pools.create({ name }),
    onSuccess: (p) => {
      setCreated(p);
      setName("");
      qc.invalidateQueries({ queryKey: ["worker-pools"] });
    },
  });
  const remove = useMutation({
    mutationFn: (id: string) => pools.remove(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["worker-pools"] }),
  });

  return (
    <Card>
      <div className="mb-1 text-[13px] font-medium">Worker pools</div>
      <div className="mb-2 text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
        A pool groups workers and carries the registration token agents authenticate with. Create one,
        then start an agent with its token.
      </div>

      {created && (
        <div
          className="mb-3 rounded-base p-3"
          style={{ border: "1px solid var(--color-accent)", backgroundColor: "var(--color-bg-base)" }}
        >
          <div className="mb-1 text-[12px] font-medium">
            Agent token for “{created.name}” — copy it now, it won't be shown again.
          </div>
          <div className="flex items-center gap-2">
            <code
              className="font-data flex-1 truncate rounded-base px-2 py-1 text-[12px]"
              style={{ border: "1px solid var(--color-border)" }}
            >
              {created.token}
            </code>
            <Button type="button" onClick={() => navigator.clipboard?.writeText(created.token)}>
              Copy
            </Button>
            <Button type="button" onClick={() => setCreated(null)}>
              Dismiss
            </Button>
          </div>
        </div>
      )}

      <div className="flex flex-col gap-2">
        {(data ?? []).map((p) => (
          <div
            key={p.id}
            className="rounded-base flex items-center justify-between gap-3 p-3"
            style={{ backgroundColor: "var(--color-bg-base)", border: "1px solid var(--color-border)" }}
          >
            <span className="text-[13px] font-medium">{p.name}</span>
            <DeleteButton label={`Delete pool ${p.name}`} onClick={() => remove.mutate(p.id)} />
          </div>
        ))}
        {data && data.length === 0 && (
          <span className="font-data text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
            No pools yet.
          </span>
        )}
      </div>

      <form
        className="mt-3 flex items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
      >
        <Field label="Pool name">
          <TextInput value={name} onChange={(e) => setName(e.target.value)} required />
        </Field>
        <Button type="submit" variant="accent" disabled={create.isPending || !name}>
          Create pool
        </Button>
      </form>
      {(create.isError || remove.isError) && (
        <div className="mt-2 font-data text-[12px]" style={{ color: "var(--color-state-failed)" }}>
          {((create.error ?? remove.error) as Error).message}
        </div>
      )}
    </Card>
  );
}

export function HealthPage() {
  const isAdmin = useIsAdmin();
  const [logWorker, setLogWorker] = useState("");
  const [diagWorker, setDiagWorker] = useState<string | null>(null);
  const { data: h } = useQuery({
    queryKey: ["health"],
    queryFn: observability.health,
    refetchInterval: 3000,
  });

  return (
    <div className="flex flex-col gap-4">
      <PageTitle>Workers &amp; health</PageTitle>
      {h && (
        <>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
            <Metric label="Status" value={h.status} tone={h.status === "ok" ? "var(--color-state-finished)" : "var(--color-state-failed)"} />
            <Metric label="Database" value={h.checks.database} tone={h.checks.database === "ok" ? "var(--color-state-finished)" : "var(--color-state-failed)"} />
            <Metric label="Workers online" value={`${h.workers.online}/${h.workers.total}`} />
            <Metric label="Runs active / queued" value={`${h.runs.active} / ${h.runs.queued}`} />
            <Metric label="Warnings+errors" value={h.log_buffer.recent_warn_error} />
          </div>

          <Card>
            <div className="mb-1 text-[13px] font-medium">Workers</div>
            <div className="mb-3 text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
              Grouped by pool. A run is routed to a worker whose labels cover the environment's
              labels — use labels to pin tool, version or architecture. Installed tool versions are
              visible via Diagnostics.
            </div>
            {h.workers.items.length === 0 ? (
              <p className="text-[13px]" style={{ color: "var(--color-text-secondary)" }}>
                No workers registered. Start an agent with a pool token.
              </p>
            ) : (
              <div className="flex flex-col gap-4">
                {groupByPool(h.workers.items).map(([pool, items]) => (
                  <div key={pool ?? "(no pool)"}>
                    <div className="mb-1 flex flex-wrap items-center gap-2">
                      <span className="text-[13px] font-medium">{pool ?? "(no pool)"}</span>
                      <span className="font-data text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
                        {items.filter((w) => w.online).length}/{items.length} online
                      </span>
                      <LabelChips labels={items[0]?.pool_labels ?? null} />
                    </div>
                    <table className="w-full text-left font-data text-[12px]">
                      <thead>
                        <tr style={{ color: "var(--color-text-secondary)" }}>
                          <th className="py-1 pr-4">NAME</th>
                          <th className="py-1 pr-4">LABELS</th>
                          <th className="py-1 pr-4">STATUS</th>
                          <th className="py-1 pr-4">LAST HEARTBEAT</th>
                          <th className="py-1 pr-4">AGENT</th>
                          <th className="py-1">DEBUG</th>
                        </tr>
                      </thead>
                      <tbody>
                        {items.map((w) => (
                          <tr key={w.id}>
                            <td className="py-1 pr-4">{w.name}</td>
                            <td className="py-1 pr-4">
                              <LabelChips labels={w.labels} />
                            </td>
                            <td className="py-1 pr-4" style={{ color: w.online ? "var(--color-state-finished)" : "var(--color-state-queued)" }}>
                              {w.online ? "online" : w.status}
                            </td>
                            <td className="py-1 pr-4">{w.seconds_since_heartbeat != null ? `${w.seconds_since_heartbeat}s ago` : "—"}</td>
                            <td className="py-1 pr-4">{w.version ?? "—"}</td>
                            <td className="flex gap-2 py-1">
                              <Button
                                onClick={() => setDiagWorker(w.id)}
                                disabled={!w.online}
                                title={w.online ? undefined : "Worker offline — it can't pick up a diagnostics request"}
                              >
                                Diagnostics
                              </Button>
                              <Button onClick={() => setLogWorker(w.id)}>Logs</Button>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ))}
              </div>
            )}
          </Card>
        </>
      )}
      {isAdmin && <PoolsPanel />}
      {diagWorker && <DiagnosticsPanel workerId={diagWorker} onClose={() => setDiagWorker(null)} />}
      <LogsPanel workerId={logWorker} setWorkerId={setLogWorker} />
    </div>
  );
}
