import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import type { LogChunk } from "@/api/types";
import { ADMIN, ALICE, BOB, TIERS, makeEnv, makeRun } from "./__fixtures__/factories";
import { downstreamOf } from "./cascade";
import { severity, summarizeEnv } from "./envSummary";
import { buildLogModel, planLines } from "./logModel";
import { fourEyesOf, runRights, whoMayConfirm } from "./permissions";
import { buildSegments } from "./phases";
import { accountedLines, groupByAction, parsePlan } from "./planParse";
import { formatCounts, outcomeOf } from "./runOutcome";
import { age, formatDuration, isoLocal, relative } from "./time";

const prodTier = TIERS[2];

describe("runOutcome", () => {
  it("tells applied from no changes (gap 4)", () => {
    expect(outcomeOf(makeRun({ state: "finished", confirmed_at: "2026-10-10T20:01:00Z" }))).toBe("applied");
    expect(outcomeOf(makeRun({ state: "finished", plan_summary: { add: 0, change: 0, destroy: 0 } }))).toBe(
      "no_changes",
    );
  });
  it("names the other outcomes", () => {
    expect(outcomeOf(makeRun({ state: "finished", type: "proposed" }))).toBe("planned");
    expect(outcomeOf(makeRun({ state: "finished", type: "destroy", confirmed_at: "x" }))).toBe("destroyed");
    expect(outcomeOf(makeRun({ state: "failed", confirmed_at: "x" }))).toBe("failed_during_apply");
    expect(outcomeOf(makeRun({ state: "failed" }))).toBe("failed");
    expect(outcomeOf(makeRun({ state: "planning" }))).toBeNull();
  });
  it("formats counts with a true minus", () => {
    expect(formatCounts({ add: 4, change: 0, destroy: 1 })).toBe("+4 ~0 −1");
  });
});

describe("phases", () => {
  const statuses = (r: ReturnType<typeof makeRun>) => buildSegments(r).map((s) => `${s.key}:${s.status}`);

  it("never shows a finished run as active (the v1 spinner bug)", () => {
    const done = buildSegments(makeRun({ state: "finished", confirmed_at: "x" }));
    expect(done.every((s) => s.status === "done")).toBe(true);
  });
  it("marks decision and apply as not needed on an empty plan", () => {
    const r = makeRun({ state: "finished", plan_summary: { add: 0, change: 0, destroy: 0 } });
    expect(statuses(r)).toEqual(["prepare:done", "plan:done", "decision:not_needed", "apply:not_needed"]);
  });
  it("waits at the decision when unconfirmed", () => {
    expect(statuses(makeRun())).toEqual(["prepare:done", "plan:done", "decision:waiting", "apply:pending"]);
  });
  it("shows where a run failed: an after_plan check", () => {
    const r = makeRun({
      state: "failed",
      plan_summary: null,
      error: "after_plan",
      check_results: { results: [{ name: "policy", status: "fail", stage: "after_plan" }] },
    });
    expect(statuses(r)).toEqual([
      "prepare:done",
      "plan:done",
      "check:failed",
      "decision:not_run",
      "apply:not_run",
    ]);
  });
  it("shows a failure during apply on the apply segment", () => {
    const r = makeRun({ state: "failed", confirmed_at: "x" });
    expect(statuses(r).at(-1)).toBe("apply:failed");
  });
  it("stops a discarded run at the decision", () => {
    const seg = buildSegments(makeRun({ state: "discarded" }));
    expect(seg.find((s) => s.key === "decision")).toMatchObject({ status: "not_run", meta: "discarded" });
  });
  it("has no decision for proposed runs and a run segment for commands", () => {
    expect(buildSegments(makeRun({ type: "proposed", state: "planning" })).map((s) => s.key)).toEqual([
      "prepare",
      "plan",
    ]);
    expect(buildSegments(makeRun({ type: "command", state: "running" })).map((s) => s.key)).toEqual([
      "prepare",
      "run",
    ]);
  });
  it("puts durations and counts in the metadata", () => {
    const seg = buildSegments(makeRun(), { planning: 14 });
    expect(seg.find((s) => s.key === "plan")?.meta).toBe("14s · +1 ~1 −1");
    const auto = buildSegments(makeRun({ state: "applying", confirmed_at: "x" }));
    expect(auto.find((s) => s.key === "decision")?.meta).toBe("auto");
  });
});

describe("envSummary (DESIGN §2.3)", () => {
  const env = makeEnv();
  it("keeps a pending decision visible behind a newer canceled run (audit C1)", () => {
    const pending = makeRun({ id: "r1", state: "unconfirmed", created_at: "2026-10-10T20:00:00Z" });
    const canceled = makeRun({ id: "r2", state: "canceled", created_at: "2026-10-10T20:05:00Z" });
    const s = summarizeEnv(env, [canceled, pending]);
    expect(s.primary?.id).toBe("r1");
    expect(s.active).toBe(true);
  });
  it("ignores PR plans and drift checks once idle", () => {
    const applied = makeRun({
      id: "a",
      state: "finished",
      confirmed_at: "x",
      created_at: "2026-10-10T19:00:00Z",
    });
    const pr = makeRun({ id: "p", type: "proposed", state: "failed", created_at: "2026-10-10T20:00:00Z" });
    const drift = makeRun({
      id: "d",
      type: "proposed",
      triggered_by: "schedule",
      state: "finished",
      created_at: "2026-10-10T21:00:00Z",
    });
    const s = summarizeEnv(makeEnv({ drift_status: "drifted" }), [drift, pr, applied]);
    expect(s.primary?.id).toBe("a");
    expect(s.outcome).toBe("applied");
    expect(s.modifiers.drift).toBe("drifted");
    expect(s.deployedSha).toBe("2af0351c9e");
  });
  it("marks an active PR run and stale envs", () => {
    const s = summarizeEnv(makeEnv({ stale: true }), [makeRun({ type: "proposed", state: "planning" })]);
    expect(s.modifiers.pr).toBe(true);
    expect(s.modifiers.stale).toEqual({ commitsAhead: null, affectsProjectRoot: null });
  });
  it("sorts the worst first", () => {
    const waiting = summarizeEnv(env, [makeRun()]);
    const fine = summarizeEnv(env, [makeRun({ state: "finished", confirmed_at: "x" })]);
    const never = summarizeEnv(env, []);
    expect([never, fine, waiting].sort((a, b) => severity(a) - severity(b))).toEqual([waiting, fine, never]);
  });
});

describe("permissions (mirrors the API gates)", () => {
  const prodEnv = makeEnv();
  const run = makeRun(); // triggered manually by bob on a four-eyes tier

  it("refuses bob twice, with the API's wording", () => {
    const r = runRights(run, prodEnv, prodTier, BOB);
    expect(r.confirm.visible).toBe(true);
    expect(r.confirm.reasons).toEqual([
      "approver role required",
      "you triggered this run; four-eyes requires someone else to confirm",
    ]);
    expect(r.discard.reasons).toEqual([]);
    expect(r.needsYou).toBe(false);
  });
  it("lets alice confirm: needs you, with friction", () => {
    const r = runRights(run, prodEnv, prodTier, ALICE);
    expect(r.confirm.reasons).toEqual([]);
    expect(r.needsYou).toBe(true);
    expect(r.highRisk).toBe(true);
    expect(whoMayConfirm(run, prodEnv, r, "bob@dev.local")).toBe(
      "an approver with tier prod other than bob@dev.local",
    );
  });
  it("blocks everyone on a mocked run unless the env allows it", () => {
    const mocked = makeRun({ used_mocks: true });
    const r = runRights(mocked, prodEnv, prodTier, ADMIN);
    expect(r.blockedForAll).toHaveLength(1);
    expect(r.needsYou).toBe(false);
    expect(whoMayConfirm(mocked, prodEnv, r)).toBeNull();
    expect(runRights(mocked, makeEnv({ allow_mock_apply: true }), prodTier, ADMIN).confirm.reasons).toEqual(
      [],
    );
  });
  it("fails closed on a tier missing from the catalog", () => {
    const fe = fourEyesOf(run, makeEnv({ tier: "ghost" }), undefined);
    expect(fe.required).toBe(true);
  });
  it("requires the destroy right, and adds friction even off four-eyes tiers", () => {
    const destroy = makeRun({ type: "destroy" });
    const staging = makeEnv({ tier: "staging" });
    const r = runRights(destroy, staging, TIERS[1], ALICE);
    expect(r.confirm.reasons).toEqual(["destroy permission required"]);
    expect(r.highRisk).toBe(true);
  });
  it("only offers actions that fit the state", () => {
    const r = runRights(makeRun({ state: "finished", confirmed_at: "x" }), prodEnv, prodTier, ALICE);
    expect([r.confirm.visible, r.discard.visible, r.cancel.visible, r.replan.visible]).toEqual([
      false,
      false,
      false,
      true,
    ]);
    expect(runRights(makeRun({ state: "queued" }), prodEnv, prodTier, ALICE).cancel.visible).toBe(true);
  });
});

describe("planParse", () => {
  const fixture = readFileSync(resolve(process.cwd(), "src/lib/__fixtures__/plan-mixed.txt"), "utf8")
    .replace(/\n$/, "")
    .split("\n");

  it("parses the seeded mixed plan, destroys first", () => {
    const p = parsePlan(fixture);
    expect(p.resources.map((r) => `${r.action} ${r.address}`)).toEqual([
      "destroy terraform_data.nat_gateway",
      "create terraform_data.subnet_db",
      "update terraform_data.subnet_public",
    ]);
    expect(p.resources[0].notes).toEqual(["# (because terraform_data.nat_gateway is not in configuration)"]);
    expect(p.resources[2].changedAttributes).toEqual(["input", "output"]);
    expect(p.summary).toBe("Plan: 1 to add, 1 to change, 1 to destroy.");
    expect(p.outputs[0]).toBe("Changes to Outputs:");
    expect(groupByAction(p).map((g) => g.action)).toEqual(["destroy", "create", "update"]);
  });
  it("never drops a line", () => {
    const p = parsePlan(fixture);
    expect(accountedLines(p)).toBe(fixture.length);
    expect(p.unparsed).toEqual([]);
  });
  it("detects tag-only updates and forced replacements", () => {
    const p = parsePlan([
      "  # aws_s3_bucket.logs will be updated in-place",
      '  ~ resource "aws_s3_bucket" "logs" {',
      "      ~ tags     = {",
      '          ~ "owner" = "a" -> "b"',
      "        }",
      "      ~ tags_all = {",
      "        }",
      "    }",
      "",
      "  # aws_instance.web must be replaced",
      '-/+ resource "aws_instance" "web" {',
      '      ~ ami = "ami-1" -> "ami-2" # forces replacement',
      "    }",
    ]);
    expect(p.resources[0]).toMatchObject({ action: "update", tagOnly: true });
    // The replace opener starts at column 0 ("-/+ resource"), not the two-space indent: still parsed.
    expect(p.resources[1]).toMatchObject({ action: "replace", forcesReplacement: ["ami"] });
    expect(p.resources[1].body).toHaveLength(3);
    expect(p.unparsed).toEqual([]);
    expect(accountedLines(p)).toBe(13);
  });
  it("keeps what it does not understand", () => {
    const p = parsePlan(["Warning: something new", "  # x.y has changed", '  ~ resource "x" "y" {', "    }"]);
    expect(p.unparsed).toEqual(["Warning: something new"]);
    expect(p.resources[0].action).toBe("other");
  });
});

describe("logModel", () => {
  const chunk = (
    phase: string,
    seq: number,
    section: string | null,
    msgs: string[],
    t = "20:00:00",
  ): LogChunk => ({
    phase,
    seq,
    section,
    lines: msgs.map((msg) => ({ t, msg })),
  });

  it("orders by lifecycle, not by phase name (audit C3)", () => {
    const m = buildLogModel([
      chunk("checking", 0, "hook:policy", ["DENY x"]),
      chunk("planning", 1, null, ["b"], "20:00:09"),
      chunk("planning", 0, null, ["a"], "20:00:01"),
    ]);
    expect(m.lines.map((l) => l.msg)).toEqual(["a", "b", "DENY x"]);
    expect(m.sections.map((s) => s.title)).toEqual(["Init & plan", "Hook · policy"]);
    expect(m.lines.map((l) => l.id)).toEqual(["planning.1", "planning.2", "checking.1"]);
    expect(m.durations.planning).toBe(8);
    expect(m.cursor).toEqual({ planning: 1, checking: 0 });
  });
  it("finds the first error", () => {
    const m = buildLogModel([
      chunk("planning", 0, null, ["ok", "│ Error: Reference to undeclared input variable"]),
    ]);
    expect(m.firstError).toBe(1);
    expect(m.sections[0].hasError).toBe(true);
  });
  it("extracts the plan section in order", () => {
    expect(
      planLines([chunk("planning", 3, "plan", ["second"]), chunk("planning", 2, "plan", ["first"])]),
    ).toEqual(["first", "second"]);
  });
});

describe("cascade", () => {
  const edges = [
    {
      id: "e1",
      upstream: "net-prod",
      downstream: "app-prod",
      policy: "on_output_change",
      references: 1,
      has_mock: true,
    },
    {
      id: "e2",
      upstream: "app-prod",
      downstream: "dns-prod",
      policy: "always",
      references: 1,
      has_mock: false,
    },
    {
      id: "e3",
      upstream: "net-prod",
      downstream: "audit-prod",
      policy: "never",
      references: 1,
      has_mock: false,
    },
  ];
  it("walks downstream, propagating uncertainty and skipping `never`", () => {
    expect(downstreamOf("net-prod", edges).map((s) => [s.envId, s.likelihood, s.depth])).toEqual([
      ["app-prod", "plans_if_outputs_change", 1],
      ["dns-prod", "plans_if_outputs_change", 2],
    ]);
    expect(downstreamOf("app-prod", edges)[0].likelihood).toBe("will_plan");
  });
});

describe("time", () => {
  const now = new Date("2026-10-10T20:00:00");
  it("formats relative, age, durations and ISO", () => {
    expect(relative(new Date("2026-10-10T19:48:00"), now)).toBe("12m ago");
    expect(relative(new Date("2026-10-08T19:48:00"), now)).toBe("2026-10-08 19:48");
    expect(age(new Date("2026-10-10T17:00:00"), now)).toBe("3h");
    expect(formatDuration(65)).toBe("1m 05s");
    expect(isoLocal(new Date("2026-01-02T03:04:00"))).toBe("2026-01-02 03:04");
  });
});
