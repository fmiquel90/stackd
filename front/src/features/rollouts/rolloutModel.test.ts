import { describe, expect, it } from "vitest";
import type { RolloutMember } from "@/api/resources";
import { ttlText } from "./RolloutPage";
import { MEMBER_STATUS, ROLLOUT_STATE, byLevel, memberLook, progress, summaryText } from "./rolloutModel";

const member = (over: Partial<RolloutMember>): RolloutMember => ({
  environment_id: "e",
  stack_name: "s",
  env_name: "prod-euw3",
  tier: "prod",
  protected: false,
  level: 0,
  status: "pending",
  run_id: null,
  run_state: null,
  plan_summary: null,
  auto_confirmed: false,
  waiting_reason: null,
  expected_mocks: [],
  ...over,
});

describe("rollout model", () => {
  it("groups members by level, sorted by stack", () => {
    const levels = byLevel([
      member({ stack_name: "app", level: 1 }),
      member({ stack_name: "network", level: 0 }),
      member({ stack_name: "api", level: 1 }),
    ]);
    expect(levels.map((l) => l.map((m) => m.stack_name))).toEqual([["network"], ["api", "app"]]);
  });

  it("summarises a plan, imports included", () => {
    expect(summaryText(member({}))).toBeNull();
    expect(summaryText(member({ plan_summary: { add: 3, change: 0, destroy: 0 } }))).toBe("+3 ~0 -0");
    expect(summaryText(member({ plan_summary: { add: 1, import: 2 } }))).toBe("+1 ~0 -0 ⇣2");
  });

  it("shows a pending member as not started before the approval and at level 0", () => {
    expect(memberLook(member({ level: 1 }), "awaiting_approval").label).toBe("not started");
    expect(memberLook(member({ level: 0 }), "running").label).toBe("not started");
    expect(memberLook(member({ level: 1 }), "running").label).toBe("waiting for its upstreams");
  });

  it("states the approval lifetime the API reports", () => {
    expect(ttlText(86400)).toBe("24 hours");
    expect(ttlText(7 * 86400)).toBe("7 days");
    expect(ttlText(5400)).toBe("90 minutes");
  });

  it("counts progress", () => {
    expect(progress({ members: [member({ status: "done" }), member({})] })).toBe("1 / 2 done");
  });

  it("never relies on colour alone: every state has a label and a glyph", () => {
    for (const look of [...Object.values(ROLLOUT_STATE), ...Object.values(MEMBER_STATUS)]) {
      expect(look.label).toBeTruthy();
      expect(look.glyph).toBeTruthy();
    }
  });
});
