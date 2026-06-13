import "@xyflow/react/dist/style.css";
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
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
import { graphApi, runs, stacks, type GraphEdge, type GraphNode } from "@/api/resources";
import { useEntityStreams } from "@/api/stream";
import { StateBadge } from "@/components/StateBadge";
import { Button, Card, PageTitle } from "@/components/ui";

interface EnvNodeData extends Record<string, unknown> {
  label: string;
  tier: string;
  envId: string;
}

function EnvNode({ data }: NodeProps) {
  const d = data as EnvNodeData;
  // WS (subscribed once at page level) invalidates ["env-runs"] on every transition; the poll
  // is only a reconnection fallback, so it can be slow.
  const { data: list } = useQuery({
    queryKey: ["env-runs", d.envId],
    queryFn: () => runs.list(d.envId),
    refetchInterval: 30000,
  });
  const latest = list?.[0];
  return (
    <div
      className="rounded-base px-3 py-2"
      style={{ backgroundColor: "var(--color-bg-surface)", border: "1px solid var(--color-border)", minWidth: 170 }}
    >
      <Handle type="target" position={Position.Left} style={{ background: "var(--color-border)" }} />
      <div className="text-[13px] font-medium">{d.label}</div>
      <div className="font-data mt-1 mb-1.5 text-[11px]" style={{ color: "var(--color-text-secondary)" }}>
        tier={d.tier}
      </div>
      {latest ? (
        <StateBadge state={latest.state} mocked={latest.used_mocks} />
      ) : (
        <span className="font-data text-[11px]" style={{ color: "var(--color-text-secondary)" }}>
          no runs
        </span>
      )}
      <Handle type="source" position={Position.Right} style={{ background: "var(--color-border)" }} />
    </div>
  );
}

const nodeTypes = { env: EnvNode };

function layout(nodes: GraphNode[], edges: GraphEdge[], label: (n: GraphNode) => string) {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "LR", nodesep: 30, ranksep: 110 });
  g.setDefaultEdgeLabel(() => ({}));
  nodes.forEach((n) => g.setNode(n.id, { width: 180, height: 80 }));
  edges.forEach((e) => g.setEdge(e.upstream, e.downstream));
  dagre.layout(g);

  const rfNodes: Node[] = nodes.map((n) => {
    const p = g.node(n.id);
    return {
      id: n.id,
      type: "env",
      position: { x: p.x - 90, y: p.y - 40 },
      data: { label: label(n), tier: n.tier, envId: n.id },
    };
  });
  const rfEdges: Edge[] = edges.map((e) => ({
    id: e.id,
    source: e.upstream,
    target: e.downstream,
    label: e.references ? `${e.references} ref${e.references > 1 ? "s" : ""}` : undefined,
    style: e.has_mock
      ? { stroke: "var(--color-mock)", strokeDasharray: "6 4" }
      : { stroke: "var(--color-border)" },
    labelStyle: { fill: "var(--color-text-secondary)", fontSize: 11 },
  }));
  return { rfNodes, rfEdges };
}

export function GraphPage() {
  const [view, setView] = useState<"graph" | "list">("graph");
  const [filter, setFilter] = useState("");
  const { data } = useQuery({ queryKey: ["graph"], queryFn: graphApi.get });
  const stackList = useQuery({ queryKey: ["stacks"], queryFn: stacks.list });

  // One socket subscribed to every env channel: a cascade lights up the whole graph live.
  const envSubs = useMemo(() => (data?.nodes ?? []).map((n) => `environment:${n.id}`), [data]);
  useEntityStreams(envSubs, [["env-runs"]]);

  const stackName = (sid: string) => stackList.data?.find((s) => s.id === sid)?.name ?? sid.slice(0, 6);
  const label = (n: GraphNode) => `${stackName(n.stack_id)}/${n.name}`;

  const { rfNodes, rfEdges } = useMemo(() => {
    if (!data) return { rfNodes: [], rfEdges: [] };
    const q = filter.toLowerCase();
    const nodes = q ? data.nodes.filter((n) => label(n).toLowerCase().includes(q)) : data.nodes;
    const ids = new Set(nodes.map((n) => n.id));
    const edges = data.edges.filter((e) => ids.has(e.upstream) && ids.has(e.downstream));
    return layout(nodes, edges, label);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, filter, stackList.data]);

  if (!data) return <p className="font-data text-[12px]">Loading…</p>;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <PageTitle>Dependency graph</PageTitle>
        <div className="flex items-center gap-2">
          <input
            placeholder="filter by env name"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            className="font-data rounded-base px-2 py-1.5 text-[13px]"
            style={{ border: "1px solid var(--color-border)", backgroundColor: "var(--color-bg-base)", color: "var(--color-text-primary)" }}
          />
          <Button onClick={() => setView(view === "graph" ? "list" : "graph")}>
            {view === "graph" ? "List view" : "Graph view"}
          </Button>
        </div>
      </div>

      {view === "graph" ? (
        <div
          style={{ height: "calc(100vh - 180px)", border: "1px solid var(--color-border)", borderRadius: 4, background: "var(--color-bg-base)" }}
        >
          <ReactFlow nodes={rfNodes} edges={rfEdges} nodeTypes={nodeTypes} fitView proOptions={{ hideAttribution: true }}>
            <Background color="var(--color-border)" gap={20} />
            <Controls showInteractive={false} />
          </ReactFlow>
        </div>
      ) : (
        <Card>
          {/* Accessible adjacency table — the a11y-equivalent of the graph (DESIGN §5.4). */}
          {data.edges.length === 0 ? (
            <p className="text-[13px]" style={{ color: "var(--color-text-secondary)" }}>
              No dependencies defined.
            </p>
          ) : (
            <table className="w-full text-left font-data text-[12px]">
              <thead>
                <tr style={{ color: "var(--color-text-secondary)" }}>
                  <th className="py-1 pr-4">UPSTREAM</th>
                  <th className="py-1 pr-4" />
                  <th className="py-1 pr-4">DOWNSTREAM</th>
                  <th className="py-1 pr-4">REFS</th>
                  <th className="py-1">POLICY</th>
                </tr>
              </thead>
              <tbody>
                {data.edges.map((e) => {
                  const up = data.nodes.find((n) => n.id === e.upstream);
                  const down = data.nodes.find((n) => n.id === e.downstream);
                  return (
                    <tr key={e.id}>
                      <td className="py-1 pr-4">{up ? label(up) : e.upstream.slice(0, 8)}</td>
                      <td className="py-1 pr-4" aria-hidden>→</td>
                      <td className="py-1 pr-4">{down ? label(down) : e.downstream.slice(0, 8)}</td>
                      <td className="py-1 pr-4">
                        {e.references}
                        {e.has_mock ? <span style={{ color: "var(--color-mock)" }}> · MOCK</span> : null}
                      </td>
                      <td className="py-1" style={{ color: "var(--color-text-secondary)" }}>{e.policy}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </Card>
      )}
    </div>
  );
}
