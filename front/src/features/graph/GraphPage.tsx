import "@xyflow/react/dist/style.css";
import { useState } from "react";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Background,
  Controls,
  Handle,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import dagre from "dagre";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { ApiError } from "@/api/client";
import { dependenciesApi, graphApi, runs, type Dependency, type GraphEdge } from "@/api/resources";
import { useSpaces } from "@/app/SpaceContext";
import { useSession } from "@/auth/session";
import { useSpaceEnvs, type EnvEntry } from "@/features/data/spaceEnvs";
import { EnvOutputs } from "@/features/environments/EnvInputs";
import { cellLabel } from "@/features/overview/Matrix";
import { EnvCell } from "@/identity/EnvCell";
import { Button } from "@/ui/Button";
import { Dialog, DialogContent, DialogTrigger, SheetContent } from "@/ui/overlays";
import { Banner, EmptyState, Field, Input, NativeSelect, Skeleton, Table, Td, Th } from "@/ui/primitives";
import { cn } from "@/ui/cn";

// The dependency graph (DESIGN §5.4): nodes are env summaries (§2.3), edges carry the outputs they
// pass, dashed magenta only when the downstream's run actually consumed a mock. The list view is a
// full equivalent. Filters and view live in the URL.

interface EdgeInfo {
  edge: GraphEdge;
  outputs: string[];
  inputs: string[];
  mockConsumed: boolean;
  mockDefined: boolean;
}

interface NodeData extends Record<string, unknown> {
  entry: EnvEntry;
  selected: boolean;
}

function EnvNode({ data }: NodeProps) {
  const { entry, selected } = data as NodeData;
  return (
    <div className={cn("w-[230px] rounded-base border bg-surface", selected ? "border-ring" : "border-line")}>
      <Handle type="target" position={Position.Left} className="!border-control !bg-surface" />
      <EnvCell
        summary={entry.summary}
        needsYou={!!entry.rights?.needsYou}
        title={
          <span className="font-data text-[12px] font-semibold">
            {entry.stack.name} / {entry.env.name}
            <span className="ml-1 font-normal text-muted">· {entry.env.tier}</span>
          </span>
        }
      />
      <Handle type="source" position={Position.Right} className="!border-control !bg-surface" />
    </div>
  );
}
const nodeTypes = { env: EnvNode };

function edgeInfos(
  edges: GraphEdge[],
  deps: Map<string, Dependency[]>,
  entries: Map<string, EnvEntry>,
): EdgeInfo[] {
  return edges.map((edge) => {
    const dep = deps.get(edge.downstream)?.find((d) => d.id === edge.id);
    const refs = dep?.references ?? [];
    const provenance = entries.get(edge.downstream)?.summary.primary?.variable_provenance ?? {};
    // The run provenance names the injected variable; match the input with or without TF_VAR_.
    const consumed = refs.some(
      (r) => provenance[r.input_name] === "mock" || provenance[`TF_VAR_${r.input_name}`] === "mock",
    );
    return {
      edge,
      outputs: refs.map((r) => r.output_name),
      inputs: refs.map((r) => r.input_name),
      mockConsumed: consumed,
      mockDefined: refs.some((r) => r.has_mock),
    };
  });
}

function edgeLabel(e: EdgeInfo): string {
  if (!e.outputs.length) return `${e.edge.references} ref${e.edge.references === 1 ? "" : "s"}`;
  return e.outputs.length > 2
    ? `${e.outputs.slice(0, 2).join(", ")} +${e.outputs.length - 2}`
    : e.outputs.join(", ");
}

function layout(entries: EnvEntry[], infos: EdgeInfo[], selectedId: string | null) {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "LR", nodesep: 28, ranksep: 220 });
  g.setDefaultEdgeLabel(() => ({}));
  entries.forEach((e) => g.setNode(e.env.id, { width: 230, height: 92 }));
  infos.forEach((i) => g.setEdge(i.edge.upstream, i.edge.downstream));
  dagre.layout(g);
  // Topological-ish order (left to right, top to bottom) = the Tab order of the nodes (DESIGN §5.4).
  const ordered = [...entries].sort((a, b) => {
    const pa = g.node(a.env.id);
    const pb = g.node(b.env.id);
    return pa.x - pb.x || pa.y - pb.y;
  });
  const nodes: Node[] = ordered.map((e) => {
    const p = g.node(e.env.id);
    return {
      id: e.env.id,
      type: "env",
      position: { x: p.x - 115, y: p.y - 46 },
      data: { entry: e, selected: e.env.id === selectedId } satisfies NodeData,
      ariaLabel: cellLabel(e),
    };
  });
  const edges: Edge[] = infos.map((i) => ({
    id: i.edge.id,
    source: i.edge.upstream,
    target: i.edge.downstream,
    label: `${edgeLabel(i)}${i.mockConsumed ? " · ◆ mock" : i.mockDefined ? " · ◇" : ""}`,
    className: i.mockConsumed ? "edge-mock" : "edge-real",
    ariaLabel: `${i.edge.upstream} to ${i.edge.downstream}: ${edgeLabel(i)}${i.mockConsumed ? ", mocked" : ""}`,
  }));
  return { nodes, edges };
}

export function GraphPage() {
  const { current: space } = useSpaces();
  const me = useSession().data;
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const data = useSpaceEnvs(space?.id, me);
  const graph = useQuery({ queryKey: ["graph"], queryFn: graphApi.get });

  const set = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) {
      if (!v) next.delete(k);
      else next.set(k, v);
    }
    setParams(next, { replace: true });
  };
  const view = params.get("view") === "list" ? "list" : "graph";
  const envFilter = (params.get("env") ?? "").toLowerCase();
  const tierFilter = params.get("tier") ?? "";
  const selectedId = params.get("node");

  const entries = new Map(data.entries.map((e) => [e.env.id, e]));
  const inSpace = (graph.data?.edges ?? []).filter(
    (e) => entries.has(e.upstream) && entries.has(e.downstream),
  );
  const downstreams = [...new Set(inSpace.map((e) => e.downstream))];
  const depQueries = useQueries({
    queries: downstreams.map((id) => ({
      queryKey: ["dependencies", id],
      queryFn: () => dependenciesApi.list(id),
      staleTime: 60_000,
    })),
  });
  const deps = new Map(downstreams.map((id, i) => [id, depQueries[i]?.data ?? []]));

  const visible = data.entries.filter(
    (e) =>
      (!envFilter || `${e.stack.name}/${e.env.name}`.toLowerCase().includes(envFilter)) &&
      (!tierFilter || e.env.tier === tierFilter),
  );
  const visibleIds = new Set(visible.map((e) => e.env.id));
  const infos = edgeInfos(
    inSpace.filter((e) => visibleIds.has(e.upstream) && visibleIds.has(e.downstream)),
    deps,
    entries,
  );
  // Laid out every render: dagre on a few dozen nodes is negligible, and memo keys would be fragile.
  const { nodes, edges } = layout(visible, infos, selectedId);

  if (!space || data.loading || graph.isLoading) return <Skeleton className="h-[60vh] w-full" />;
  const tiersInSpace = [...new Set(data.entries.map((e) => e.env.tier))];
  const selected = selectedId ? entries.get(selectedId) : undefined;
  const canWrite = !!me && me.role !== "reader";

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-end gap-2">
        <h1 className="mr-auto text-title font-semibold">Dependency graph</h1>
        <label className="flex flex-col gap-1">
          <span className="eyebrow text-muted">Filter</span>
          <Input
            value={params.get("env") ?? ""}
            onChange={(e) => set({ env: e.target.value })}
            placeholder="stack/env"
            className="w-48"
          />
        </label>
        <label className="flex flex-col gap-1">
          <span className="eyebrow text-muted">Tier</span>
          <NativeSelect value={tierFilter} onChange={(e) => set({ tier: e.target.value })}>
            <option value="">All tiers</option>
            {tiersInSpace.map((t) => (
              <option key={t}>{t}</option>
            ))}
          </NativeSelect>
        </label>
        <Button onClick={() => set({ view: view === "graph" ? "list" : null })}>
          {view === "graph" ? "List view" : "Graph view"}
        </Button>
        {canWrite && <LinkStacksDialog stackOptions={data.stacks} />}
      </div>
      <p className="text-[12px] text-muted">
        Edges name the outputs they pass. <span className="text-mocked">◆ mock</span> = the downstream's
        current run used a mock value; ◇ = a mock is defined but unused. Run groups are not exposed by the API
        yet (AUDIT A5).
      </p>

      {data.entries.length === 0 ? (
        <EmptyState title="No environment in this space yet." />
      ) : view === "graph" ? (
        <div className="graph-canvas h-[calc(100vh-240px)] min-h-[420px] rounded-base border border-line bg-base">
          <ReactFlow
            nodes={nodes}
            edges={edges}
            nodeTypes={nodeTypes}
            fitView
            minZoom={0.2}
            nodesDraggable={false}
            nodesConnectable={false}
            proOptions={{ hideAttribution: true }}
            onNodeClick={(_, n) => set({ node: n.id })}
          >
            <Background gap={24} />
            <Controls showInteractive={false} />
          </ReactFlow>
        </div>
      ) : (
        <AdjacencyList infos={infos} entries={entries} onNode={(id) => set({ node: id })} />
      )}

      <Dialog open={!!selected} onOpenChange={(v) => !v && set({ node: null })}>
        {selected && (
          <SheetContent title={`${selected.stack.name} / ${selected.env.name}`}>
            <NodeDrawer
              entry={selected}
              infos={inSpace.length ? edgeInfos(inSpace, deps, entries) : []}
              entries={entries}
              envHref={`/s/${encodeURIComponent(space.name)}/stacks/${encodeURIComponent(selected.stack.name)}/envs/${encodeURIComponent(selected.env.name)}`}
              canWrite={canWrite}
              onPlanned={(id) => navigate(`/runs/${id}`)}
            />
          </SheetContent>
        )}
      </Dialog>
    </div>
  );
}

function AdjacencyList({
  infos,
  entries,
  onNode,
}: {
  infos: EdgeInfo[];
  entries: Map<string, EnvEntry>;
  onNode: (id: string) => void;
}) {
  const name = (id: string) => {
    const e = entries.get(id);
    return e ? `${e.stack.name}/${e.env.name}` : id.slice(0, 8);
  };
  if (!infos.length) return <EmptyState title="No dependency matches these filters." />;
  return (
    <div className="overflow-x-auto rounded-base border border-line bg-surface">
      <Table>
        <caption className="sr-only">
          Dependencies: upstream environment, downstream environment, outputs passed, trigger policy, mocks
        </caption>
        <thead>
          <tr>
            <Th>Upstream</Th>
            <Th>Downstream</Th>
            <Th>Outputs → inputs</Th>
            <Th>Policy</Th>
            <Th>Mock</Th>
          </tr>
        </thead>
        <tbody>
          {[...infos]
            .sort((a, b) => name(a.edge.upstream).localeCompare(name(b.edge.upstream)))
            .map((i) => (
              <tr key={i.edge.id}>
                <Td>
                  <button
                    type="button"
                    className="font-data text-[12px] underline underline-offset-2"
                    onClick={() => onNode(i.edge.upstream)}
                  >
                    {name(i.edge.upstream)}
                  </button>
                </Td>
                <Td>
                  <button
                    type="button"
                    className="font-data text-[12px] underline underline-offset-2"
                    onClick={() => onNode(i.edge.downstream)}
                  >
                    {name(i.edge.downstream)}
                  </button>
                </Td>
                <Td className="font-data text-[12px]">
                  {i.outputs.length
                    ? i.outputs.map((o, k) => `${o} → ${i.inputs[k]}`).join(", ")
                    : `${i.edge.references} reference(s)`}
                </Td>
                <Td className="font-data text-[12px] text-muted">{i.edge.policy}</Td>
                <Td className="font-data text-[12px]">
                  {i.mockConsumed ? (
                    <span className="text-mocked">◆ consumed</span>
                  ) : i.mockDefined ? (
                    <span className="text-muted">◇ defined</span>
                  ) : (
                    "—"
                  )}
                </Td>
              </tr>
            ))}
        </tbody>
      </Table>
    </div>
  );
}

function NodeDrawer({
  entry,
  infos,
  entries,
  envHref,
  canWrite,
  onPlanned,
}: {
  entry: EnvEntry;
  infos: EdgeInfo[];
  entries: Map<string, EnvEntry>;
  envHref: string;
  canWrite: boolean;
  onPlanned: (runId: string) => void;
}) {
  const plan = useMutation({
    mutationFn: () => runs.trigger(entry.env.id),
    onSuccess: (r) => onPlanned(r.id),
  });
  const name = (id: string) => {
    const e = entries.get(id);
    return e ? `${e.stack.name}/${e.env.name}` : id.slice(0, 8);
  };
  const inbound = infos.filter((i) => i.edge.downstream === entry.env.id);
  const outbound = infos.filter((i) => i.edge.upstream === entry.env.id);
  const run = entry.summary.primary;
  return (
    <div className="flex flex-col gap-4">
      <EnvCell summary={entry.summary} needsYou={!!entry.rights?.needsYou} />
      <div className="flex flex-wrap gap-2">
        <Link
          to={envHref}
          className="inline-flex h-8 items-center rounded-base border border-control px-3 hover:bg-surface"
        >
          Open environment
        </Link>
        {run && (
          <Link
            to={`/runs/${run.id}`}
            className="inline-flex h-8 items-center rounded-base border border-control px-3 hover:bg-surface"
          >
            Open run
          </Link>
        )}
        {canWrite && !entry.summary.active && (
          <Button variant="primary" loading={plan.isPending} onClick={() => plan.mutate()}>
            Plan
          </Button>
        )}
      </div>
      {plan.error && (
        <Banner tone="failed" title="The API refused the plan">
          {plan.error instanceof ApiError ? (plan.error.detail ?? plan.error.title) : String(plan.error)}
        </Banner>
      )}
      <section>
        <h3 className="eyebrow mb-1 text-muted">Receives from</h3>
        {inbound.length ? (
          <ul className="font-data text-[12px]">
            {inbound.map((i) => (
              <li key={i.edge.id}>
                {name(i.edge.upstream)}{" "}
                <span className="text-muted">— {i.outputs.join(", ") || `${i.edge.references} ref(s)`}</span>
                {i.mockConsumed && <span className="text-mocked"> · ◆ mock used</span>}
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-[12px] text-muted">No upstream dependency.</p>
        )}
      </section>
      <section>
        <h3 className="eyebrow mb-1 text-muted">Feeds</h3>
        {outbound.length ? (
          <ul className="font-data text-[12px]">
            {outbound.map((i) => (
              <li key={i.edge.id}>
                {name(i.edge.downstream)}{" "}
                <span className="text-muted">
                  — {i.outputs.join(", ") || `${i.edge.references} ref(s)`} · {i.edge.policy}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-[12px] text-muted">Nothing depends on this environment.</p>
        )}
      </section>
      <section>
        <h3 className="eyebrow mb-1 text-muted">Published outputs</h3>
        <EnvOutputs envId={entry.env.id} />
      </section>
    </div>
  );
}

/** Bulk-wire two stacks by output/input names (SPECS §9) — a secondary action in a dialog. */
function LinkStacksDialog({ stackOptions }: { stackOptions: { id: string; name: string }[] }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [downstream, setDownstream] = useState("");
  const [upstream, setUpstream] = useState("");
  const [policy, setPolicy] = useState("on_output_change");
  const link = useMutation({
    mutationFn: () =>
      dependenciesApi.linkByName(downstream, { upstream_stack_id: upstream, trigger_policy: policy }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["graph"] });
      qc.invalidateQueries({ queryKey: ["dependencies"] });
    },
  });
  return (
    <Dialog open={open} onOpenChange={(v) => (setOpen(v), v || link.reset())}>
      <DialogTrigger asChild>
        <Button>Link stacks…</Button>
      </DialogTrigger>
      <DialogContent
        title="Link two stacks by name"
        description="For every environment name both stacks share, wire each upstream output to the downstream input of the same name."
      >
        <form
          className="flex flex-col gap-3"
          onSubmit={(e) => {
            e.preventDefault();
            link.mutate();
          }}
        >
          <Field label="Downstream stack (receives)">
            <NativeSelect value={downstream} onChange={(e) => setDownstream(e.target.value)} required>
              <option value="">select…</option>
              {stackOptions.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </NativeSelect>
          </Field>
          <Field label="Upstream stack (publishes)">
            <NativeSelect value={upstream} onChange={(e) => setUpstream(e.target.value)} required>
              <option value="">select…</option>
              {stackOptions
                .filter((s) => s.id !== downstream)
                .map((s) => (
                  <option key={s.id} value={s.id}>
                    {s.name}
                  </option>
                ))}
            </NativeSelect>
          </Field>
          <Field label="Trigger policy">
            <NativeSelect value={policy} onChange={(e) => setPolicy(e.target.value)}>
              <option value="on_output_change">on_output_change</option>
              <option value="always">always</option>
              <option value="never">never</option>
            </NativeSelect>
          </Field>
          {link.data && (
            <Banner title={`Created ${link.data.created} reference${link.data.created === 1 ? "" : "s"}.`} />
          )}
          {link.error && (
            <Banner tone="failed" title="Not linked">
              {link.error instanceof ApiError ? (link.error.detail ?? link.error.title) : String(link.error)}
            </Banner>
          )}
          <div className="flex justify-end gap-2">
            <Button variant="ghost" onClick={() => setOpen(false)}>
              Close
            </Button>
            <Button
              type="submit"
              variant="primary"
              loading={link.isPending}
              disabled={!downstream || !upstream}
            >
              Link
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
