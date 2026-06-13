import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { observability, workers } from "@/api/resources";
import type { LogEntry } from "@/api/types";
import { Button, Card, PageTitle, Select, TextInput } from "@/components/ui";

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

const LEVEL_COLOR: Record<string, string> = {
  ERROR: "var(--color-state-failed)",
  CRITICAL: "var(--color-state-failed)",
  WARNING: "var(--color-state-unconfirmed)",
  INFO: "var(--color-state-running)",
  DEBUG: "var(--color-text-secondary)",
};

function DiagnosticsPanel({ workerId, onClose }: { workerId: string; onClose: () => void }) {
  const request = useMutation({ mutationFn: () => workers.requestDiagnostics(workerId) });
  const { data } = useQuery({
    queryKey: ["diagnostics", workerId],
    queryFn: () => workers.diagnostics(workerId),
    refetchInterval: (q) => (q.state.data?.status === "done" || q.state.data?.status === "failed" ? false : 1500),
  });
  // Fire a fresh request when the panel opens.
  useEffect(() => {
    request.mutate();
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
          <div key={i} className="whitespace-pre-wrap">
            <span style={{ color: "var(--color-text-secondary)" }}>{e.ts.slice(11, 19)} </span>
            <span style={{ color: LEVEL_COLOR[e.level] ?? "var(--color-text-secondary)" }}>{e.level.padEnd(5)} </span>
            <span style={{ color: "var(--color-text-secondary)" }}>{e.logger} </span>
            {e.event ? <span style={{ color: "var(--color-accent)" }}>{e.event} </span> : null}
            {e.msg}
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

export function HealthPage() {
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
            <Metric label="Warnings+errors" value={h.log_buffer.recent_warn_error} tone={h.log_buffer.recent_warn_error > 0 ? "var(--color-state-unconfirmed)" : undefined} />
          </div>

          <Card>
            <div className="mb-2 text-[13px] font-medium">Workers</div>
            {h.workers.items.length === 0 ? (
              <p className="text-[13px]" style={{ color: "var(--color-text-secondary)" }}>
                No workers registered. Start an agent with a pool token.
              </p>
            ) : (
              <table className="w-full text-left font-data text-[12px]">
                <thead>
                  <tr style={{ color: "var(--color-text-secondary)" }}>
                    <th className="py-1 pr-4">NAME</th>
                    <th className="py-1 pr-4">STATUS</th>
                    <th className="py-1 pr-4">LAST HEARTBEAT</th>
                    <th className="py-1 pr-4">VERSION</th>
                    <th className="py-1">DEBUG</th>
                  </tr>
                </thead>
                <tbody>
                  {h.workers.items.map((w) => (
                    <tr key={w.id}>
                      <td className="py-1 pr-4">{w.name}</td>
                      <td className="py-1 pr-4" style={{ color: w.online ? "var(--color-state-finished)" : "var(--color-state-queued)" }}>
                        {w.online ? "online" : w.status}
                      </td>
                      <td className="py-1 pr-4">{w.seconds_since_heartbeat != null ? `${w.seconds_since_heartbeat}s ago` : "—"}</td>
                      <td className="py-1 pr-4">{w.version ?? "—"}</td>
                      <td className="flex gap-2 py-1">
                        <Button onClick={() => setDiagWorker(w.id)}>Diagnostics</Button>
                        <Button onClick={() => setLogWorker(w.id)}>Logs</Button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Card>
        </>
      )}
      {diagWorker && <DiagnosticsPanel workerId={diagWorker} onClose={() => setDiagWorker(null)} />}
      <LogsPanel workerId={logWorker} setWorkerId={setLogWorker} />
    </div>
  );
}
