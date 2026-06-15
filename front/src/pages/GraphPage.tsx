import "@xyflow/react/dist/style.css";
import { Fragment, useMemo, useState } from "react";
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
import { Link, useNavigate } from "react-router-dom";
import { dependenciesApi, graphApi, runs, stacks, type GraphEdge, type GraphNode } from "@/api/resources";
import { useEntityStreams } from "@/api/stream";
import { StateBadge } from "@/components/StateBadge";
import { Button, Card, PageTitle } from "@/components/ui";

interface EnvNodeData extends Record<string, unknown> {
  label: string;
  tier: string;
  envId: string;
  stackId: string;
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
      style={{ backgroundColor: "var(--color-bg-surface)", border: "1px solid var(--color-border)", minWidth: 170, cursor: "pointer" }}
      title="Open this stack"
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

// The /graph edge only carries a reference *count*; the per-reference detail (which upstream output
// feeds which downstream input, and whether it's mocked) lives on the downstream env's dependency.
function EdgeRefs({ downstreamEnvId, depId }: { downstreamEnvId: string; depId: string }) {
  const { data } = useQuery({
    queryKey: ["dependencies", downstreamEnvId],
    queryFn: () => dependenciesApi.list(downstreamEnvId),
  });
  if (!data) return <span className="font-data text-[12px]">Loading…</span>;
  const dep = data.find((d) => d.id === depId);
  if (!dep || dep.references.length === 0) {
    return (
      <span className="font-data text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
        No reference details.
      </span>
    );
  }
  return (
    <table className="w-full text-left font-data text-[12px]">
      <thead>
        <tr style={{ color: "var(--color-text-secondary)" }}>
          <th className="py-1 pr-4">UPSTREAM OUTPUT</th>
          <th className="py-1 pr-4" />
          <th className="py-1 pr-4">DOWNSTREAM INPUT</th>
          <th className="py-1">SOURCE</th>
        </tr>
      </thead>
      <tbody>
        {dep.references.map((r) => (
          <tr key={`${r.output_name}-${r.input_name}`}>
            <td className="py-1 pr-4">{r.output_name}</td>
            <td className="py-1 pr-4" aria-hidden>→</td>
            <td className="py-1 pr-4">{r.input_name}</td>
            <td className="py-1">
              {r.has_mock ? (
                <span style={{ color: "var(--color-mock)" }}>mock available</span>
              ) : (
                <span style={{ color: "var(--color-text-secondary)" }}>real output</span>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

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
      data: { label: label(n), tier: n.tier, envId: n.id, stackId: n.stack_id },
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
  const navigate = useNavigate();
  const [view, setView] = useState<"graph" | "list">("graph");
  const [filter, setFilter] = useState("");
  // The dependency edge whose references are being inspected (graph edge click / list row toggle).
  const [openEdge, setOpenEdge] = useState<{ depId: string; downstreamEnvId: string; label: string } | null>(null);
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
        <>
          <div
            style={{ height: "calc(100vh - 180px)", border: "1px solid var(--color-border)", borderRadius: 4, background: "var(--color-bg-base)" }}
          >
            <ReactFlow
              nodes={rfNodes}
              edges={rfEdges}
              nodeTypes={nodeTypes}
              fitView
              proOptions={{ hideAttribution: true }}
              onNodeClick={(_, node) => navigate(`/stacks/${(node.data as EnvNodeData).stackId}`)}
              onEdgeClick={(_, edge) => {
                const up = data.nodes.find((n) => n.id === edge.source);
                const down = data.nodes.find((n) => n.id === edge.target);
                setOpenEdge({
                  depId: edge.id,
                  downstreamEnvId: edge.target,
                  label: `${up ? label(up) : edge.source.slice(0, 6)} → ${down ? label(down) : edge.target.slice(0, 6)}`,
                });
              }}
            >
              <Background color="var(--color-border)" gap={20} />
              <Controls showInteractive={false} />
            </ReactFlow>
          </div>
          {openEdge && (
            <Card>
              <div className="mb-2 flex items-center justify-between">
                <span className="text-[13px] font-medium">
                  References · <span className="font-data">{openEdge.label}</span>
                </span>
                <Button onClick={() => setOpenEdge(null)}>Close</Button>
              </div>
              <EdgeRefs downstreamEnvId={openEdge.downstreamEnvId} depId={openEdge.depId} />
            </Card>
          )}
        </>
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
                  const expanded = openEdge?.depId === e.id;
                  return (
                    <Fragment key={e.id}>
                      <tr>
                        <td className="py-1 pr-4">
                          {up ? (
                            <Link to={`/stacks/${up.stack_id}`} style={{ color: "var(--color-text-primary)" }}>
                              {label(up)}
                            </Link>
                          ) : (
                            e.upstream.slice(0, 8)
                          )}
                        </td>
                        <td className="py-1 pr-4" aria-hidden>→</td>
                        <td className="py-1 pr-4">
                          {down ? (
                            <Link to={`/stacks/${down.stack_id}`} style={{ color: "var(--color-text-primary)" }}>
                              {label(down)}
                            </Link>
                          ) : (
                            e.downstream.slice(0, 8)
                          )}
                        </td>
                        <td className="py-1 pr-4">
                          {/* Toggle the per-reference detail (output → input, mocked or real). */}
                          <button
                            type="button"
                            className="ui-btn"
                            aria-expanded={expanded}
                            style={{ color: "var(--color-accent)" }}
                            onClick={() =>
                              setOpenEdge(
                                expanded
                                  ? null
                                  : { depId: e.id, downstreamEnvId: e.downstream, label: "" },
                              )
                            }
                          >
                            {e.references} ref{e.references > 1 ? "s" : ""}
                          </button>
                          {e.has_mock ? <span style={{ color: "var(--color-mock)" }}> · MOCK</span> : null}
                        </td>
                        <td className="py-1" style={{ color: "var(--color-text-secondary)" }}>{e.policy}</td>
                      </tr>
                      {expanded && (
                        <tr>
                          <td colSpan={5} className="pb-2 pl-4">
                            <EdgeRefs downstreamEnvId={e.downstream} depId={e.id} />
                          </td>
                        </tr>
                      )}
                    </Fragment>
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
