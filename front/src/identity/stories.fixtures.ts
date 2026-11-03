import type { LogChunk, Run } from "@/api/types";
import { makeEnv, makeRun } from "@/lib/__fixtures__/factories";

// Named run scenarios shared by the identity stories (the seeded demo, every state DESIGN §3.2 names).

const T0 = "2026-10-10T20:00:00Z";
const ago = (min: number) => new Date(Date.now() - min * 60_000).toISOString();

export const SCENARIOS: { name: string; run: Run }[] = [
  {
    name: "queued",
    run: makeRun({ state: "queued", plan_summary: null, claimed_at: null, created_at: ago(1) }),
  },
  { name: "planning", run: makeRun({ state: "planning", plan_summary: null, created_at: ago(1) }) },
  {
    name: "checking",
    run: makeRun({
      state: "checking",
      check_results: { results: [{ name: "tfsec", status: "ok", stage: "after_plan" }] },
    }),
  },
  { name: "unconfirmed", run: makeRun({ created_at: ago(12) }) },
  { name: "applying (auto-confirmed)", run: makeRun({ state: "applying", confirmed_at: T0 }) },
  {
    name: "applied",
    run: makeRun({
      state: "finished",
      confirmed_at: T0,
      confirmed_by_user_id: "user-alice",
      finished_at: ago(120),
    }),
  },
  {
    name: "no changes",
    run: makeRun({
      state: "finished",
      plan_summary: { add: 0, change: 0, destroy: 0 },
      finished_at: ago(60),
    }),
  },
  {
    name: "failed at check",
    run: makeRun({
      state: "failed",
      plan_summary: null,
      error: "after_plan",
      check_results: {
        results: [
          { name: "tfsec", status: "ok", stage: "after_plan" },
          { name: "policy: require owner tag", status: "fail", stage: "after_plan", detail: "exit 1" },
        ],
      },
      finished_at: ago(6),
    }),
  },
  {
    name: "failed in plan",
    run: makeRun({ state: "failed", plan_summary: null, error: "Reference to undeclared input variable" }),
  },
  {
    name: "failed during apply",
    run: makeRun({ state: "failed", confirmed_at: T0, error: "apply exited 1" }),
  },
  { name: "discarded", run: makeRun({ state: "discarded", finished_at: ago(30) }) },
  { name: "canceled", run: makeRun({ state: "canceled", plan_summary: null, claimed_at: null }) },
  {
    name: "proposed (PR) planned",
    run: makeRun({ type: "proposed", state: "finished", triggered_by: "webhook" }),
  },
  {
    name: "destroy unconfirmed",
    run: makeRun({ type: "destroy", plan_summary: { add: 0, change: 0, destroy: 3 } }),
  },
  {
    name: "command running",
    run: makeRun({ type: "command", state: "running", command: { name: "state list", args: [] } }),
  },
  {
    name: "mocked unconfirmed",
    run: makeRun({ used_mocks: true, plan_summary: { add: 3, change: 0, destroy: 0 } }),
  },
];

export const run = (name: string): Run => SCENARIOS.find((s) => s.name === name)!.run;

export const ENV_PROD = makeEnv();
export const ENV_STAGING = makeEnv({ id: "env-staging", name: "staging", tier: "staging", position: 1 });

const chunk = (
  phase: string,
  seq: number,
  section: string | null,
  msgs: string[],
  t = "20:07:07",
): LogChunk => ({
  phase,
  seq,
  section,
  lines: msgs.map((msg) => ({ t, msg })),
});

const ESC = String.fromCharCode(27);

export const LOG_FAILED_HOOK: LogChunk[] = [
  chunk("planning", 0, null, [
    "",
    `${ESC}[1mInitializing the backend...${ESC}[0m`,
    `Successfully configured the backend "http"!`,
    `${ESC}[32m${ESC}[1mOpenTofu has been successfully initialized!${ESC}[0m`,
    "terraform_data.zone: Plan to create",
    "terraform_data.record_api: Plan to create",
    "Plan: 3 to add, 0 to change, 0 to destroy.",
  ]),
  chunk(
    "checking",
    0,
    "hook:policy: require owner tag",
    [
      "checking 3 planned resources against tag policy...",
      `${ESC}[31mDENY${ESC}[0m terraform_data.zone: missing required tag owner`,
      `${ESC}[31mDENY${ESC}[0m terraform_data.record_api: missing required tag owner`,
      "│ Error: 2 policy violations",
    ],
    "20:07:09",
  ),
];

export const LOG_ANSI: LogChunk[] = [
  chunk(
    "planning",
    0,
    null,
    [30, 31, 32, 33, 34, 35, 36, 37, 90, 91, 92, 93, 94, 95, 96, 97].map(
      (c) => `${ESC}[${c}mcolour ${c}${ESC}[0m  ${ESC}[1;${c}mbold ${c}${ESC}[0m`,
    ),
  ),
];

export function bigLog(lines: number): LogChunk[] {
  const out: LogChunk[] = [];
  for (let seq = 0; seq * 100 < lines; seq++) {
    out.push(
      chunk(
        "planning",
        seq,
        seq === 0 ? null : seq % 50 === 0 ? "plan" : null,
        Array.from(
          { length: 100 },
          (_, i) => `aws_subnet.private[${seq * 100 + i}]: Refreshing state... [id=subnet-0f3a${seq}${i}]`,
        ),
      ),
    );
  }
  return out;
}

export function bigPlan(resources: number): string[] {
  const lines = ["OpenTofu will perform the following actions:", ""];
  for (let i = 0; i < resources; i++) {
    const action = i % 50 === 7 ? "destroyed" : i % 10 === 3 ? "updated in-place" : "created";
    const marker = action === "destroyed" ? "-" : action === "created" ? "+" : "~";
    lines.push(`  # aws_subnet.private[${i}] will be ${action}`);
    lines.push(`  ${marker} resource "aws_subnet" "private" {`);
    lines.push(`      ${marker} cidr_block = "10.0.${i % 256}.0/24"`);
    lines.push("    }", "");
  }
  lines.push(`Plan: ${resources} to add, 0 to change, 0 to destroy.`);
  return lines;
}
