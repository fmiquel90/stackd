import { describe, expect, it } from "vitest";
import type { Environment, Run, Stack } from "@/api/types";
import { ALICE, BOB, TIERS, makeEnv, makeRun } from "@/lib/__fixtures__/factories";
import { summarizeEnv } from "@/lib/envSummary";
import { runRights, whoMayConfirm } from "@/lib/permissions";
import type { EnvEntry } from "@/features/data/spaceEnvs";
import { attentionOf, envColumns, isStalePlan, matchesState } from "./attention";

const STACK = { id: "stack-network", name: "demo-network", space_id: "space-demo" } as Stack;

function entry(env: Environment, runs: Run[], me = ALICE): EnvEntry {
  const summary = summarizeEnv(env, runs);
  const tier = TIERS.find((t) => t.name === env.tier);
  const pending = summary.primary?.state === "unconfirmed" ? summary.primary : null;
  const rights = pending ? runRights(pending, env, tier, me) : null;
  return {
    stack: STACK,
    env,
    tier,
    runs,
    summary,
    rights,
    whoMayConfirm: pending && rights ? whoMayConfirm(pending, env, rights) : null,
  };
}

describe("attentionOf", () => {
  it("puts a pending run the viewer can confirm in 'needs you', and the same run in 'waiting' for bob", () => {
    const run = makeRun();
    expect(attentionOf(entry(makeEnv(), [run]), "/env")?.group).toBe("needs_you");
    const forBob = attentionOf(entry(makeEnv(), [run], BOB), "/env");
    expect(forBob?.group).toBe("waiting");
    expect(forBob?.reason).toBe("needs an approver with tier prod other than the person who triggered it");
  });
  it("blocks a mocked plan for everyone", () => {
    expect(attentionOf(entry(makeEnv(), [makeRun({ used_mocks: true })]), "/env")?.group).toBe("blocked");
  });
  it("keeps a pending prod decision visible behind a newer canceled run (audit C1)", () => {
    const pending = makeRun({ created_at: "2026-10-10T20:00:00Z" });
    const canceled = makeRun({ state: "canceled", created_at: "2026-10-10T20:05:00Z" });
    expect(attentionOf(entry(makeEnv(), [canceled, pending]), "/env")?.group).toBe("needs_you");
  });
  it("lists failures and drift, and nothing for a healthy env", () => {
    expect(attentionOf(entry(makeEnv(), [makeRun({ state: "failed" })]), "/env")?.group).toBe("failed");
    const drifted = makeEnv({ drift_status: "drifted", drift_run_id: "drift-1" });
    expect(
      attentionOf(entry(drifted, [makeRun({ state: "finished", confirmed_at: "x" })]), "/env"),
    ).toMatchObject({
      group: "drift",
      href: "/runs/drift-1?tab=plan",
    });
    expect(
      attentionOf(entry(makeEnv(), [makeRun({ state: "finished", confirmed_at: "x" })]), "/env"),
    ).toBeNull();
  });
  it("flags a plan computed on a commit the branch moved past", () => {
    expect(isStalePlan(makeRun({ commit_sha: "aaaaaaa" }), makeEnv({ head_sha: "bbbbbbb1" }))).toBe(true);
    expect(isStalePlan(makeRun({ commit_sha: "aaaaaaa" }), makeEnv({ head_sha: "aaaaaaa123" }))).toBe(false);
  });
});

describe("matrix columns and filters", () => {
  it("orders environment columns by tier position, then env position, then name", () => {
    const entries = [
      entry(makeEnv({ id: "p", name: "prod", tier: "prod", position: 2 }), []),
      entry(makeEnv({ id: "d", name: "dev", tier: "dev", position: 0 }), []),
      entry(makeEnv({ id: "q", name: "qa", tier: "staging", position: 1 }), []),
      entry(makeEnv({ id: "s", name: "staging", tier: "staging", position: 1 }), []),
    ];
    expect(envColumns(entries, TIERS)).toEqual(["dev", "qa", "staging", "prod"]);
  });
  it("filters on state", () => {
    const e = entry(makeEnv(), [makeRun()]);
    expect(matchesState(e, "needs_you", "/env")).toBe(true);
    expect(matchesState(e, "failed", "/env")).toBe(false);
    expect(matchesState(e, "attention", "/env")).toBe(true);
    expect(matchesState(entry(makeEnv(), [makeRun({ state: "planning" })]), "running", "/env")).toBe(true);
  });
});
