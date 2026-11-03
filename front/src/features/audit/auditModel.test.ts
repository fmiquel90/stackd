import { describe, expect, it } from "vitest";
import type { AuditEvent, Space, Stack } from "@/api/types";
import { makeEnv } from "@/lib/__fixtures__/factories";
import {
  actionGroups,
  apiParams,
  contextSummary,
  isConsequential,
  isSignIn,
  targetOf,
  type Lookup,
} from "./auditModel";

const stack = { id: "stack-network", space_id: "space-demo", name: "demo-network" } as Stack;
const space = { id: "space-demo", name: "demo" } as Space;
const env = makeEnv();
const lookup: Lookup = { stacks: [stack], envs: [env], spaces: [space], workers: [], users: [] };

function ev(over: Partial<AuditEvent>): AuditEvent {
  return {
    id: "e1",
    actor_kind: "user",
    actor_email: "alice@dev.local",
    action: "run.confirmed",
    target_kind: "run",
    target_id: "01a1276c-e6dd-7d3d-bd8f-7a969ce3880b",
    context: null,
    created_at: "2026-10-10T20:00:00Z",
    ...over,
  };
}

describe("audit model", () => {
  it("resolves a run target with its environment, linked", () => {
    const t = targetOf(ev({ context: { environment_id: env.id } }), lookup);
    expect(t).toEqual({
      label: "run 01a1276c · demo-network/prod",
      to: "/runs/01a1276c-e6dd-7d3d-bd8f-7a969ce3880b",
    });
  });

  it("links an environment to its page, and names a deleted stack from the context", () => {
    expect(targetOf(ev({ target_kind: "environment", target_id: env.id }), lookup)?.to).toBe(
      "/s/demo/stacks/demo-network/envs/prod",
    );
    expect(
      targetOf(ev({ target_kind: "stack", target_id: "gone", context: { name: "e2e-x" } }), lookup),
    ).toEqual({
      label: "stack e2e-x",
      gone: true,
    });
  });

  it("summarises the context in words and never shows credential-like keys", () => {
    const e = ev({
      action: "run.applied",
      context: {
        from: "applying",
        to: "finished",
        commit: "125304e54d95",
        environment_id: env.id,
        token: "x",
        value: "y",
      },
    });
    expect(contextSummary(e, targetOf(e, lookup))).toEqual(["applying → finished", "commit 125304e"]);
    const v = ev({
      action: "variable.created",
      target_kind: "variable",
      context: { name: "region", sensitive: true, scope: "env" },
    });
    expect(contextSummary(v, targetOf(v, lookup))).toEqual(["sensitive", "scope env"]);
  });

  it("flags consequential rows and sign-ins", () => {
    expect(isConsequential("run.applied")).toBe(true);
    expect(isConsequential("run.destroy_triggered")).toBe(true);
    expect(isConsequential("run.triggered")).toBe(false);
    expect(isSignIn("auth.login")).toBe(true);
    expect(isSignIn("auth.domain_denied")).toBe(false);
  });

  it("groups actions by domain, merging unknown ones seen in events", () => {
    const groups = Object.fromEntries(actionGroups(["zeta.new_thing"]));
    expect(groups.run).toContain("run.applied");
    expect(groups.zeta).toEqual(["zeta.new_thing"]);
  });

  it("maps URL filters to the API's, the cursor overriding the end date", () => {
    const f = { actor: "u1", action: "", kind: "run", target: "r1", from: "", to: "2026-10-10" };
    const p = apiParams(f);
    expect(p).toMatchObject({ actor: "u1", target_kind: "run", target_id: "r1" });
    expect(new Date(p.to).getTime()).toBe(new Date("2026-10-10T23:59:59.999").getTime());
    expect(apiParams(f, "2026-10-01T00:00:00Z").to).toBe("2026-10-01T00:00:00Z");
  });
});
