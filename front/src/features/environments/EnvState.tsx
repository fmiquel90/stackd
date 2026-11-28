import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { stateApi, type StateLock } from "@/api/resources";
import { useLiveSignals } from "@/api/stream";
import type { Environment } from "@/api/types";
import { Button } from "@/ui/Button";
import { EmptyState, RelativeTime, Skeleton, Table, Td, Th } from "@/ui/primitives";
import { TypeNameDialog } from "@/ui/TypeNameDialog";
import { shortId } from "@/lib/ids";

// Managed state versions (SPECS §11) and the held state lock with the admin-only force-unlock,
// behind the typed env name (DESIGN §5.7). The state lock is the `state_locks` row a job's tofu
// takes (§11.2), not `env.locked` — the env's own lock, shown in the page header.

function LockHolder({ lock }: { lock: StateLock }) {
  const operation = lock.operation?.replace(/^OperationType/, "").toLowerCase();
  return (
    <>
      held by{" "}
      {lock.run_id ? (
        <Link to={`/runs/${lock.run_id}`} className="link font-data text-[12px]">
          run {shortId(lock.run_id)}
        </Link>
      ) : (
        <span>no run (an import session, or a lock taken before holder runs were recorded)</span>
      )}
      {operation && <span className="font-data text-[12px] text-muted"> · {operation}</span>}
      {lock.who && <span className="font-data text-[12px] text-muted"> · {lock.who}</span>} since{" "}
      <RelativeTime value={lock.locked_at} className="text-[12px]" />
    </>
  );
}

export function EnvState({ env, isAdmin }: { env: Environment; isAdmin: boolean }) {
  const qc = useQueryClient();
  const versionsKey = ["state-versions", env.id];
  const lockKey = ["state-lock", env.id];
  const { data, isLoading } = useQuery({
    queryKey: versionsKey,
    queryFn: () => stateApi.versions(env.id),
    enabled: env.managed_state,
  });
  // A job's tofu takes and frees the lock with no event of its own; a run's events come close to
  // it (a run's end releases its lock, §11.2), and the 60 s poll is the safety net (DESIGN §6).
  const lock = useQuery({
    queryKey: lockKey,
    queryFn: () => stateApi.lock(env.id),
    enabled: env.managed_state,
    refetchInterval: 60_000,
  });
  useLiveSignals(env.managed_state ? [`environment:${env.id}`] : [], (s) => {
    if (s.kind !== "run_event") return;
    qc.invalidateQueries({ queryKey: lockKey });
    qc.invalidateQueries({ queryKey: versionsKey });
  });

  if (!env.managed_state)
    return (
      <EmptyState title="State is not managed by Stackd for this environment.">
        The repository's own backend holds it (see Settings → State).
      </EmptyState>
    );

  const held = lock.data ?? null;
  return (
    <div className="flex max-w-[960px] flex-col gap-3">
      <div className="flex flex-wrap items-center gap-3">
        {lock.isLoading ? (
          <Skeleton className="h-5 w-64" />
        ) : (
          <span className="text-[12px]">
            State lock:{" "}
            {held ? (
              <>
                <span className="font-data">⚿ locked</span> <LockHolder lock={held} />
              </>
            ) : (
              <span className="font-data">not held</span>
            )}
          </span>
        )}
        {isAdmin && held && (
          <TypeNameDialog
            trigger={<Button variant="secondary">Force unlock…</Button>}
            title={`Force-unlock the state of ${env.name}`}
            consequence={
              <>
                <p>
                  Lock <span className="font-data">{held.lock_id}</span> <LockHolder lock={held} />.
                </p>
                <p className="mt-2">
                  Only when the lock holder is gone for good: unlocking while a run still writes the state can
                  corrupt it.
                </p>
              </>
            }
            name={env.name}
            confirmLabel="Force unlock"
            onConfirm={() =>
              stateApi.forceUnlock(env.id).then(() => qc.invalidateQueries({ queryKey: lockKey }))
            }
          />
        )}
      </div>
      {isLoading ? (
        <Skeleton className="h-24 w-full" />
      ) : !data?.length ? (
        <EmptyState title="No state version yet.">The first apply writes one.</EmptyState>
      ) : (
        <Table>
          <thead>
            <tr>
              <Th>Serial</Th>
              <Th>Size</Th>
              <Th>Written by</Th>
              <Th>When</Th>
            </tr>
          </thead>
          <tbody>
            {data.map((v) => (
              <tr key={v.id}>
                <Td className="font-data text-[12px]">{v.serial}</Td>
                <Td className="font-data text-[12px] text-muted">{v.size_bytes} B</Td>
                <Td className="font-data text-[12px]">
                  {v.created_by_run_id ? (
                    <Link to={`/runs/${v.created_by_run_id}`} className="cell-link">
                      run {shortId(v.created_by_run_id)}
                    </Link>
                  ) : (
                    "—"
                  )}
                </Td>
                <Td className="text-[12px]">
                  <RelativeTime value={v.created_at} />
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      )}
    </div>
  );
}
