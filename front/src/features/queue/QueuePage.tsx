import type { ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import type { RunState } from "@/api/types";
import { useSpaces } from "@/app/SpaceContext";
import { RunStateBadge, StateBadge } from "@/identity/StateBadge";
import { age, formatDuration } from "@/lib/time";
import { Banner, EmptyState, Input, Skeleton, Table, Td, Th } from "@/ui/primitives";
import { blockerOf, matches, STUCK_AFTER_S, useQueueRows, waitedSeconds, type QueueRow } from "./queueModel";

// The execution queue (DESIGN §5.5) — "why isn't my run starting?". Two machine groups and a single
// line for the runs that wait on a human (they are not in the machine queue).

function EnvName({ row }: { row: QueueRow }) {
  const { list } = useSpaces();
  if (!row.stack || !row.env)
    return <span className="text-muted">environment {row.entry.environment_id.slice(0, 8)}</span>;
  const space = list.find((s) => s.id === row.stack!.space_id);
  const label = (
    <>
      {row.stack.name} <span className="text-muted">/</span> {row.env.name}
    </>
  );
  if (!space) return <span className="font-data text-[12px]">{label}</span>;
  return (
    <Link
      to={`/s/${encodeURIComponent(space.name)}/stacks/${encodeURIComponent(row.stack.name)}/envs/${encodeURIComponent(row.env.name)}`}
      className="font-data text-[12px] underline-offset-2 hover:underline"
    >
      {label}
    </Link>
  );
}

function RunCell({ row }: { row: QueueRow }) {
  return (
    <span className="inline-flex items-baseline gap-2 md:flex md:flex-col md:gap-0">
      <Link to={`/runs/${row.entry.run_id}`} className="font-data text-[12px] underline underline-offset-2">
        {row.entry.run_id.slice(0, 8)}
      </Link>
      {row.run && <span className="font-data text-[11px] text-muted">{row.run.type}</span>}
    </span>
  );
}

function State({ row }: { row: QueueRow }) {
  return row.run ? (
    <RunStateBadge run={row.run} compact />
  ) : (
    <StateBadge state={row.entry.state as RunState} outcome={null} compact />
  );
}

interface Column {
  header: string;
  cell: (r: QueueRow) => ReactNode;
}

/** A table from md up; on a phone, one card per run with the same labelled fields (DESIGN §4.3). */
function QueueTable({ rows, columns }: { rows: QueueRow[]; columns: Column[] }) {
  return (
    <>
      <div className="overflow-x-auto max-md:hidden">
        <Table>
          <thead>
            <tr>
              {columns.map((c) => (
                <Th key={c.header}>{c.header}</Th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.entry.run_id}>
                {columns.map((c) => (
                  <Td key={c.header}>{c.cell(r)}</Td>
                ))}
              </tr>
            ))}
          </tbody>
        </Table>
      </div>
      <ul className="flex flex-col gap-2 md:hidden">
        {rows.map((r) => (
          <li key={r.entry.run_id} className="rounded-base border border-line bg-surface px-3 py-2">
            <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
              {columns.map((c) => (
                <div key={c.header} className="contents">
                  <dt className="eyebrow self-center text-muted">{c.header}</dt>
                  <dd className="min-w-0">{c.cell(r)}</dd>
                </div>
              ))}
            </dl>
          </li>
        ))}
      </ul>
    </>
  );
}

export function QueuePage() {
  const { spacePath } = useSpaces();
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const { rows, loading, error, onlineWorkers } = useQueueRows();
  const now = new Date();
  const shown = rows.filter((r) => matches(r, q));
  const executing = shown.filter((r) => r.group === "executing");
  const waiting = shown.filter((r) => r.group === "waiting");
  const human = rows.filter((r) => r.group === "human").length;

  const executingColumns: Column[] = [
    { header: "Environment", cell: (r) => <EnvName row={r} /> },
    { header: "Run", cell: (r) => <RunCell row={r} /> },
    { header: "Phase", cell: (r) => <State row={r} /> },
    {
      header: "Worker",
      cell: (r) =>
        r.worker ? (
          <Link
            to={`/workers?worker=${encodeURIComponent(r.worker.name)}`}
            className="font-data text-[12px] underline underline-offset-2"
          >
            {r.worker.name}
          </Link>
        ) : (
          <span className="font-data text-[12px] text-muted">{r.entry.worker_id?.slice(0, 8) ?? "—"}</span>
        ),
    },
    {
      header: "Running for",
      cell: (r) => {
        const since = r.run?.claimed_at ?? r.run?.created_at;
        return (
          <span className="font-data text-[12px] text-muted">
            {since ? formatDuration((now.getTime() - new Date(since).getTime()) / 1000) : "—"}
          </span>
        );
      },
    },
  ];

  const waitingColumns: Column[] = [
    { header: "Environment", cell: (r) => <EnvName row={r} /> },
    { header: "Run", cell: (r) => <RunCell row={r} /> },
    { header: "State", cell: (r) => <State row={r} /> },
    {
      header: "Waiting for",
      cell: (r) => {
        const waited = waitedSeconds(r, now);
        if (waited === null) return <span className="font-data text-[12px] text-muted">—</span>;
        const long = waited >= STUCK_AFTER_S;
        return (
          <span className={long ? "font-data text-[12px] text-primary" : "font-data text-[12px] text-muted"}>
            {age(r.run!.created_at, now)}
            {long && <span className="text-muted"> · over 2 min</span>}
          </span>
        );
      },
    },
    {
      header: "Why it has not started",
      cell: (r) => {
        const b = blockerOf(r, rows, onlineWorkers);
        return (
          <span className="text-[13px]">
            {b.text}
            {b.link && (
              <>
                {" → "}
                <Link to={b.link.to} className="link font-data text-[12px]">
                  {b.link.label}
                </Link>
              </>
            )}
          </span>
        );
      },
    },
  ];

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <h1 className="text-title font-semibold">Queue</h1>
        <label className="flex flex-col gap-1 max-md:w-full">
          <span className="eyebrow text-muted">Filter</span>
          <Input
            type="search"
            value={q}
            placeholder="stack, env, worker, pool"
            onChange={(e) => {
              const next = new URLSearchParams(params);
              if (e.target.value) next.set("q", e.target.value);
              else next.delete("q");
              setParams(next, { replace: true });
            }}
            className="w-[260px] max-md:w-full"
          />
        </label>
      </div>

      {error && (
        <Banner tone="failed" role="alert" title="The queue could not be loaded.">
          {error.message}
        </Banner>
      )}

      {human > 0 && (
        <p className="text-ui">
          <Link to={spacePath()} className="link">
            {human} run{human > 1 ? "s" : ""} await{human > 1 ? "" : "s"} a human decision → Overview
          </Link>
          <span className="text-muted"> — not in the machine queue until confirmed.</span>
        </p>
      )}

      {loading ? (
        <Skeleton className="h-32 w-full" />
      ) : (
        <>
          <section aria-labelledby="q-exec" className="flex flex-col gap-2">
            <h2 id="q-exec" className="text-section font-semibold">
              Executing{" "}
              <span className="font-data text-[12px] font-normal text-muted">{executing.length}</span>
            </h2>
            {executing.length === 0 ? (
              <p className="text-muted">No run is executing{q ? " for this filter" : ""}.</p>
            ) : (
              <QueueTable rows={executing} columns={executingColumns} />
            )}
          </section>

          <section aria-labelledby="q-wait" className="flex flex-col gap-2">
            <h2 id="q-wait" className="text-section font-semibold">
              Waiting for a worker{" "}
              <span className="font-data text-[12px] font-normal text-muted">{waiting.length}</span>
            </h2>
            {waiting.length === 0 ? (
              rows.length === 0 && !q ? (
                <EmptyState title="The queue is empty.">
                  Nothing is executing or waiting for a worker.
                </EmptyState>
              ) : (
                <p className="text-muted">No run is waiting{q ? " for this filter" : ""}.</p>
              )
            ) : (
              <QueueTable rows={waiting} columns={waitingColumns} />
            )}
          </section>
        </>
      )}
    </div>
  );
}
