import type { GraphEdge } from "@/api/resources";

// Cascade preview (DESIGN §5.1, §5.2): which downstream envs an apply on `envId` can trigger, from
// the dependency graph (`GET /graph`) — the today-compatible stand-in for run groups (AUDIT A5).

export type CascadeLikelihood = "will_plan" | "plans_if_outputs_change";

export interface CascadeStep {
  envId: string;
  /** Upstream env this step is reached from. */
  via: string;
  depth: number;
  likelihood: CascadeLikelihood;
  policy: string;
  edgeId: string;
  /** A reference on this edge has a mock defined (not necessarily consumed). */
  mockDefined: boolean;
}

/**
 * Breadth-first walk of the outgoing edges. `never` edges stop the walk; a step behind an
 * `on_output_change` edge is only "possible", and so is everything after it.
 */
export function downstreamOf(envId: string, edges: GraphEdge[]): CascadeStep[] {
  const steps: CascadeStep[] = [];
  const seen = new Set([envId]);
  let frontier: { id: string; certain: boolean }[] = [{ id: envId, certain: true }];
  let depth = 0;
  while (frontier.length) {
    depth++;
    const next: { id: string; certain: boolean }[] = [];
    for (const node of frontier) {
      for (const e of edges.filter((x) => x.upstream === node.id && x.policy !== "never")) {
        if (seen.has(e.downstream)) continue;
        seen.add(e.downstream);
        const certain = node.certain && e.policy === "always";
        steps.push({
          envId: e.downstream,
          via: node.id,
          depth,
          likelihood: certain ? "will_plan" : "plans_if_outputs_change",
          policy: e.policy,
          edgeId: e.id,
          mockDefined: e.has_mock,
        });
        next.push({ id: e.downstream, certain });
      }
    }
    frontier = next;
  }
  return steps;
}
