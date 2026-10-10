import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { environments } from "@/api/resources";
import { VariablesEditor } from "@/components/VariablesEditor";
import { ProvenanceBadge, parseProvenance } from "@/identity/ProvenanceBadge";
import { Button } from "@/ui/Button";
import { EmptyState, Skeleton, Table, Td, Th } from "@/ui/primitives";

// The env Inputs tab (DESIGN §5.8): resolved values with provenance (read-only), env-level overrides
// (editable, override the stack-level value of the same name, SPECS §3.4), Discover inputs.
// Sensitive values: •••, never revealable.

export function EnvInputs({ envId, canWrite }: { envId: string; canWrite: boolean }) {
  const qc = useQueryClient();
  const resolved = useQuery({
    queryKey: ["resolved-variables", envId],
    queryFn: () => environments.resolvedVariables(envId),
  });
  const discover = useMutation({
    mutationFn: () => environments.discoverInputs(envId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["env-vars", envId] });
      qc.invalidateQueries({ queryKey: ["resolved-variables", envId] });
    },
  });
  const r = discover.data;

  return (
    <div className="flex max-w-[960px] flex-col gap-6">
      <section aria-labelledby="resolved-title">
        <h2 id="resolved-title" className="mb-2 text-section font-semibold">
          Resolved
        </h2>
        {resolved.isLoading ? (
          <Skeleton className="h-24 w-full" />
        ) : !resolved.data?.length ? (
          <EmptyState title="No variables resolve for this environment yet." />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Injected as</Th>
                <Th>Value</Th>
                <Th>Provenance</Th>
              </tr>
            </thead>
            <tbody>
              {resolved.data.map((v) => (
                <tr key={`${v.kind}:${v.name}`}>
                  <Td className="font-data text-[12px]">{v.injected_name}</Td>
                  <Td className="font-data text-[12px] break-all text-muted">
                    {v.sensitive || v.value === null ? "•••" : v.value}
                  </Td>
                  <Td>
                    <ProvenanceBadge provenance={parseProvenance(v.provenance)} />
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </section>

      <section aria-labelledby="overrides-title" className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 id="overrides-title" className="text-section font-semibold">
            Environment overrides
          </h2>
          {canWrite && (
            <span className="flex items-center gap-2">
              {r && (
                <span className="font-data text-[12px] text-muted" role="status">
                  {r.created.length} created{r.skipped.length ? `, ${r.skipped.length} already set` : ""} ·{" "}
                  {r.required_total} required
                </span>
              )}
              <Button onClick={() => discover.mutate()} loading={discover.isPending}>
                Discover inputs
              </Button>
            </span>
          )}
        </div>
        <p className="text-[12px] text-muted">
          Specific to this environment — overrides the stack-level variable of the same name. Discover inputs
          reads the repository's root module and scaffolds its required variables here with empty
          placeholders.
        </p>
        {discover.isError && (
          <p className="font-data text-[12px] text-st-failed">{(discover.error as Error).message}</p>
        )}
        <VariablesEditor
          queryKey={["env-vars", envId]}
          list={() => environments.variables(envId)}
          add={(body) => environments.addVariable(envId, body)}
          update={(varId, body) => environments.updateVariable(envId, varId, body)}
          remove={(varId) => environments.removeVariable(envId, varId)}
        />
      </section>
    </div>
  );
}

export function EnvOutputs({ envId }: { envId: string }) {
  const { data, isLoading } = useQuery({
    queryKey: ["env-outputs", envId],
    queryFn: () => environments.outputs(envId),
  });
  if (isLoading) return <Skeleton className="h-24 w-full" />;
  if (!data?.length)
    return (
      <EmptyState title="No outputs yet.">
        They appear after a successful apply and feed downstream environments.
      </EmptyState>
    );
  return (
    <div className="max-w-[960px]">
      <Table>
        <thead>
          <tr>
            <Th>Output</Th>
            <Th>Value</Th>
          </tr>
        </thead>
        <tbody>
          {data.map((o) => (
            <tr key={o.name}>
              <Td className="font-data text-[12px]">{o.name}</Td>
              <Td className="font-data text-[12px] break-all text-muted">
                {o.sensitive ? (
                  <span title="Sensitive outputs are never stored nor propagated (SPECS §9.1)">
                    ••• sensitive
                  </span>
                ) : (
                  (o.value ?? "")
                )}
              </Td>
            </tr>
          ))}
        </tbody>
      </Table>
    </div>
  );
}
