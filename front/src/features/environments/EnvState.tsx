import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { stateApi } from "@/api/resources";
import type { Environment } from "@/api/types";
import { Button } from "@/ui/Button";
import { EmptyState, RelativeTime, Skeleton, Table, Td, Th } from "@/ui/primitives";
import { TypeNameDialog } from "@/ui/TypeNameDialog";

// Managed state versions (SPECS §11) and the admin-only force-unlock, behind the typed env name
// (DESIGN §5.7) — v1 offered a one-click unlock to everyone.

export function EnvState({ env, isAdmin }: { env: Environment; isAdmin: boolean }) {
  const qc = useQueryClient();
  const key = ["state-versions", env.id];
  const { data, isLoading } = useQuery({
    queryKey: key,
    queryFn: () => stateApi.versions(env.id),
    enabled: env.managed_state,
  });

  if (!env.managed_state)
    return (
      <EmptyState title="State is not managed by Stackd for this environment.">
        The repository's own backend holds it (see Settings → State).
      </EmptyState>
    );

  return (
    <div className="flex max-w-[960px] flex-col gap-3">
      <div className="flex flex-wrap items-center gap-3">
        <span className="text-[12px]">
          Lock: <span className="font-data">{env.locked ? "⚿ locked" : "unlocked"}</span>
        </span>
        {isAdmin && env.locked && (
          <TypeNameDialog
            trigger={<Button variant="secondary">Force unlock…</Button>}
            title={`Force-unlock the state of ${env.name}`}
            consequence="Only when the lock holder is gone for good: unlocking while a run still writes the state can corrupt it."
            name={env.name}
            confirmLabel="Force unlock"
            onConfirm={() =>
              stateApi
                .forceUnlock(env.id)
                .then(() => qc.invalidateQueries({ queryKey: ["environment", env.id] }))
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
                    <Link to={`/runs/${v.created_by_run_id}`} className="underline underline-offset-2">
                      run {v.created_by_run_id.slice(0, 8)}
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
