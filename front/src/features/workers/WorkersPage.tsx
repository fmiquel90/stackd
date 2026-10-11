import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { observability, pools, workers } from "@/api/resources";
import type { HealthWorker, LogEntry, PoolCreated } from "@/api/types";
import { useIsAdmin } from "@/auth/session";
import { relative } from "@/lib/time";
import { Button } from "@/ui/Button";
import { Dialog, DialogContent } from "@/ui/overlays";
import {
  Badge,
  Banner,
  CopyButton,
  EmptyState,
  Field,
  Input,
  NativeSelect,
  Skeleton,
  Table,
  Td,
  Th,
} from "@/ui/primitives";
import { TypeNameDialog } from "@/ui/TypeNameDialog";
import { cn } from "@/ui/cn";

// Workers & platform health (DESIGN §5.9). Telemetry is neutral: only a failing check or an error is
// red; "ok" is not green (green means a run succeeded, §3.6).

const LONG_POLL = "/worker/v1/jobs/claim";

function Metric({ label, value, failing }: { label: string; value: string | number; failing?: boolean }) {
  return (
    <div className="flex min-w-[140px] flex-col rounded-base border border-line bg-surface px-3 py-2">
      <span className="eyebrow text-muted">{label}</span>
      <span className={cn("font-data text-[20px] leading-7", failing ? "text-st-failed" : "text-primary")}>
        {failing && <span aria-hidden>✕ </span>}
        {value}
      </span>
    </div>
  );
}

function groupByPool(items: HealthWorker[]): [string | null, HealthWorker[]][] {
  const map = new Map<string | null, HealthWorker[]>();
  for (const w of items) map.set(w.pool, [...(map.get(w.pool) ?? []), w]);
  return [...map.entries()];
}

function Labels({ labels }: { labels: Record<string, unknown> | null }) {
  const entries = Object.entries(labels ?? {});
  if (!entries.length) return <span className="text-muted">—</span>;
  return (
    <span className="flex flex-wrap gap-1">
      {entries.map(([k, v]) => (
        <Badge key={k} className="normal-case tracking-normal">
          {k}={String(v)}
        </Badge>
      ))}
    </span>
  );
}

export function WorkersPage() {
  const isAdmin = useIsAdmin();
  const [params, setParams] = useSearchParams();
  const [diag, setDiag] = useState<HealthWorker | null>(null);
  const health = useQuery({ queryKey: ["health"], queryFn: observability.health, refetchInterval: 5000 });
  const h = health.data;

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-title font-semibold">Workers &amp; health</h1>
      {health.isError && (
        <Banner tone="failed" role="alert" title="The API health endpoint does not answer.">
          {(health.error as Error).message}
        </Banner>
      )}
      {!h ? (
        <Skeleton className="h-20 w-full" />
      ) : (
        <>
          <div className="flex flex-wrap gap-2" aria-live="polite">
            <Metric label="API" value={h.status} failing={h.status !== "ok"} />
            <Metric label="Database" value={h.checks.database} failing={h.checks.database !== "ok"} />
            <Metric
              label="Workers online"
              value={`${h.workers.online}/${h.workers.total}`}
              failing={h.workers.total > 0 && h.workers.online === 0}
            />
            <Metric label="Runs active / queued" value={`${h.runs.active} / ${h.runs.queued}`} />
            <Metric label="Recent warnings" value={h.log_buffer.recent_warn_error} />
          </div>

          <section aria-labelledby="workers-title" className="flex flex-col gap-2">
            <h2 id="workers-title" className="text-section font-semibold">
              Workers
            </h2>
            <p className="text-[12px] text-muted">
              Grouped by pool. A run goes to a worker whose labels cover the environment's labels.
            </p>
            {h.workers.items.length === 0 ? (
              <EmptyState title="No worker registered.">
                Create a pool below and start an agent with its token.
              </EmptyState>
            ) : (
              groupByPool(h.workers.items).map(([pool, items]) => (
                <div key={pool ?? "(no pool)"} className="rounded-base border border-line bg-surface">
                  <div className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2">
                    <span className="font-data text-[13px] font-semibold">{pool ?? "(no pool)"}</span>
                    <span className="font-data text-[12px] text-muted">
                      {items.filter((w) => w.online).length}/{items.length} online
                    </span>
                    <Labels labels={items[0]?.pool_labels ?? null} />
                  </div>
                  <div className="overflow-x-auto">
                    <Table>
                      <thead>
                        <tr>
                          <Th>Name</Th>
                          <Th>Status</Th>
                          <Th>Last heartbeat</Th>
                          <Th>Labels</Th>
                          <Th>Agent</Th>
                          {isAdmin && <Th>Debug</Th>}
                        </tr>
                      </thead>
                      <tbody>
                        {items.map((w) => (
                          <tr key={w.id}>
                            <Td className="whitespace-nowrap font-data text-[12px]">{w.name}</Td>
                            <Td
                              className={cn(
                                "whitespace-nowrap font-data text-[12px]",
                                w.online ? "text-primary" : "text-st-failed",
                              )}
                            >
                              <span aria-hidden>{w.online ? "● " : "✕ "}</span>
                              {w.online ? "online" : "offline"}
                            </Td>
                            <Td className="font-data text-[12px] text-muted">
                              {w.last_heartbeat_at ? relative(w.last_heartbeat_at) : "never"}
                            </Td>
                            <Td>
                              <Labels labels={w.labels} />
                            </Td>
                            <Td className="font-data text-[12px] text-muted">{w.version ?? "—"}</Td>
                            {isAdmin && (
                              <Td>
                                <span className="flex gap-1">
                                  <Button
                                    size="sm"
                                    disabled={!w.online}
                                    onClick={() => setDiag(w)}
                                    title={w.online ? undefined : "Offline: it cannot run diagnostics"}
                                  >
                                    Diagnostics
                                  </Button>
                                  <Button
                                    size="sm"
                                    variant="ghost"
                                    onClick={() => setParams({ worker: w.id }, { replace: true })}
                                  >
                                    Logs
                                  </Button>
                                </span>
                              </Td>
                            )}
                          </tr>
                        ))}
                      </tbody>
                    </Table>
                  </div>
                </div>
              ))
            )}
          </section>
        </>
      )}

      {isAdmin && <PoolsPanel />}
      {isAdmin && (
        <PlatformLogs
          workerId={params.get("worker") ?? ""}
          onWorker={(v) => setParams(v ? { worker: v } : {}, { replace: true })}
        />
      )}

      <Dialog open={!!diag} onOpenChange={(v) => !v && setDiag(null)}>
        {diag && (
          <DialogContent
            wide
            title={`Diagnostics · ${diag.name}`}
            description="Runs a self-check bundle on the worker at its next heartbeat. Environment variable names only — never values."
          >
            <Diagnostics workerId={diag.id} />
          </DialogContent>
        )}
      </Dialog>
    </div>
  );
}

function Diagnostics({ workerId }: { workerId: string }) {
  const request = useMutation({ mutationFn: () => workers.requestDiagnostics(workerId) });
  const { data } = useQuery({
    queryKey: ["diagnostics", workerId],
    queryFn: () => workers.diagnostics(workerId),
    refetchInterval: (q) =>
      q.state.data?.status === "done" || q.state.data?.status === "failed" ? false : 1500,
  });
  useEffect(() => {
    // One request per opening (the dialog remounts this component).
    request.mutate();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [workerId]);

  const r = (data?.result ?? {}) as Record<string, unknown>;
  const tools = (r.tools ?? {}) as Record<string, string>;
  const disk = (r.disk ?? {}) as Record<string, number>;
  const logs = (r.recent_logs ?? []) as string[];
  const envNames = (r.env_var_names ?? []) as string[];
  if (data?.status !== "done")
    return (
      <p className="font-data text-[12px] text-muted" role="status">
        {data?.status === "failed"
          ? "The worker could not run the bundle."
          : "Waiting for the worker's next heartbeat…"}
      </p>
    );
  return (
    <div className="flex flex-col gap-2 font-data text-[12px]">
      <div>
        platform {String(r.platform)} · python {String(r.python)} · runner {String(r.runner)}
      </div>
      <div>
        tofu {tools.tofu ?? "—"} · terraform {tools.terraform ?? "—"} · git {tools.git ?? "—"}
      </div>
      <div>
        disk {disk.free_gb} GB free of {disk.total_gb} GB ({disk.used_pct}% used)
      </div>
      <div className="text-muted">{envNames.length} environment variables (names only)</div>
      <div className="max-h-[240px] overflow-auto rounded-base bg-sunken p-2">
        {logs.length ? (
          logs.map((l, i) => <div key={i}>{l}</div>)
        ) : (
          <span className="text-muted">no recent agent log</span>
        )}
      </div>
    </div>
  );
}

/** Admin pool management; a new pool's agent token is shown once (hashed at rest, never again). */
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
  return (
    <section aria-labelledby="pools-title" className="flex flex-col gap-2">
      <h2 id="pools-title" className="text-section font-semibold">
        Worker pools
      </h2>
      {created && (
        <Banner
          tone="decision"
          title={`Agent token for ${created.name} — copy it now, it will not be shown again.`}
          action={
            <Button size="sm" variant="ghost" onClick={() => setCreated(null)}>
              I copied it
            </Button>
          }
        >
          <span className="flex items-center gap-1">
            <code className="truncate rounded-base border border-line bg-sunken px-2 py-1 font-data text-[12px]">
              {created.token}
            </code>
            <CopyButton value={created.token} label="Copy the agent token" />
          </span>
        </Banner>
      )}
      <ul className="flex flex-col gap-1">
        {(data ?? []).map((p) => (
          <li
            key={p.id}
            className="flex items-center justify-between gap-3 rounded-base border border-line bg-surface px-3 py-2"
          >
            <span className="font-data text-[13px]">{p.name}</span>
            <TypeNameDialog
              trigger={
                <Button size="sm" variant="ghost">
                  Delete…
                </Button>
              }
              title={`Delete pool ${p.name}`}
              consequence="Its agents can no longer register or claim jobs with the pool token."
              name={p.name}
              confirmLabel={`Delete ${p.name}`}
              onConfirm={() =>
                pools.remove(p.id).then(() => qc.invalidateQueries({ queryKey: ["worker-pools"] }))
              }
            />
          </li>
        ))}
        {data && data.length === 0 && <li className="text-muted">No pool yet.</li>}
      </ul>
      <form
        className="flex items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
      >
        <Field label="New pool">
          <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="prod-aws" required />
        </Field>
        <Button type="submit" variant="primary" loading={create.isPending} disabled={!name}>
          Create pool
        </Button>
      </form>
      {create.isError && (
        <p className="font-data text-[12px] text-st-failed">{(create.error as Error).message}</p>
      )}
    </section>
  );
}

function HttpDetail({ e }: { e: LogEntry }) {
  if (e.method === undefined || e.path === undefined) return null;
  const longPoll = e.path === LONG_POLL;
  const slow = !longPoll && (e.duration_ms ?? 0) > 1500;
  return (
    <>
      {e.method} {e.path}
      {e.status !== undefined && (
        <span className={e.status >= 500 ? "text-st-failed" : undefined}> → {e.status}</span>
      )}
      {e.duration_ms !== undefined && (
        <span className="text-muted">
          {" "}
          · {Math.round(e.duration_ms)}ms{longPoll ? " (long-poll)" : slow ? " (slow)" : ""}
        </span>
      )}
    </>
  );
}

function PlatformLogs({ workerId, onWorker }: { workerId: string; onWorker: (v: string) => void }) {
  const [level, setLevel] = useState("");
  const [q, setQ] = useState("");
  const params: Record<string, string> = {};
  if (level) params.level = level;
  if (workerId) params.worker_id = workerId;
  if (q) params.q = q;
  const { data } = useQuery({
    queryKey: ["logs", level, workerId, q],
    queryFn: () => observability.logs(params),
    refetchInterval: 5000,
  });
  return (
    <section aria-labelledby="logs-title" className="flex flex-col gap-2">
      <h2 id="logs-title" className="text-section font-semibold">
        Platform logs
      </h2>
      <div className="flex flex-wrap items-end gap-2">
        <Field label="Level">
          <NativeSelect value={level} onChange={(e) => setLevel(e.target.value)}>
            <option value="">all</option>
            <option value="INFO">INFO and above</option>
            <option value="WARNING">WARNING and above</option>
            <option value="ERROR">ERROR and above</option>
          </NativeSelect>
        </Field>
        <Field label="Worker id">
          <Input value={workerId} onChange={(e) => onWorker(e.target.value)} />
        </Field>
        <Field label="Search">
          <Input value={q} onChange={(e) => setQ(e.target.value)} />
        </Field>
        <span className="pb-2 font-data text-[12px] text-muted">{data?.total ?? 0} matches</span>
      </div>
      <div className="max-h-[460px] overflow-auto rounded-base border border-line bg-sunken p-2 font-data text-[12px] leading-[18px]">
        {(data?.items ?? []).map((e, i) => (
          <div key={`${e.ts}-${i}`} className="whitespace-pre-wrap">
            <span className="text-muted">{e.ts.slice(11, 19)} </span>
            <span className={["ERROR", "CRITICAL"].includes(e.level) ? "text-st-failed" : "text-muted"}>
              {e.level.padEnd(8)}
            </span>
            <span className="text-muted">{e.logger} </span>
            {e.method !== undefined ? <HttpDetail e={e} /> : e.msg}
            {e.run_id ? <span className="text-muted"> run={String(e.run_id).slice(0, 8)}</span> : null}
            {e.worker_id ? (
              <span className="text-muted"> worker={String(e.worker_id).slice(0, 8)}</span>
            ) : null}
          </div>
        ))}
        {data && data.items.length === 0 && <span className="text-muted">No matching log.</span>}
      </div>
    </section>
  );
}
