import { describe, expect, it } from "vitest";
import type { HealthWorker, QueueEntry } from "@/api/types";
import { makeEnv, makeRun } from "@/lib/__fixtures__/factories";
import { blockerOf, groupOf, matches, waitedSeconds, type QueueRow } from "./queueModel";

function row(entry: Partial<QueueEntry>, extra: Partial<QueueRow> = {}): QueueRow {
  const e: QueueEntry = {
    run_id: "01a1276c-0000-7000-8000-000000000001",
    environment_id: "env-prod",
    state: "queued",
    worker_id: null,
    blocking_reason: null,
    claimable_after: null,
    claim_attempts: 0,
    ...entry,
  };
  return {
    entry: e,
    group: groupOf(e.state)!,
    run: undefined,
    stack: undefined,
    env: undefined,
    worker: undefined,
    ...extra,
  };
}

const vm = { id: "w1", name: "vm", pool: "default" } as HealthWorker;

describe("queue model", () => {
  it("groups machine states and leaves the human decision apart", () => {
    expect(groupOf("planning")).toBe("executing");
    expect(groupOf("running")).toBe("executing");
    expect(groupOf("confirmed")).toBe("waiting");
    expect(groupOf("unconfirmed")).toBe("human");
    expect(groupOf("finished")).toBeNull();
  });

  it("names the run that holds the environment", () => {
    const holder = row({ run_id: "01a1276cabcd", state: "planning", worker_id: "w1" });
    const blocked = row({ run_id: "01a1276d", blocking_reason: "active_run" });
    const b = blockerOf(blocked, [holder, blocked], 1);
    expect(b.text).toBe("another run is active on this environment");
    expect(b.link).toEqual({ to: "/runs/01a1276cabcd", label: "run 01a1276c" });
  });

  it("says when nothing can pick a claimable run up", () => {
    expect(blockerOf(row({}), [], 0).text).toBe("no worker is online");
    expect(blockerOf(row({}), [], 2).text).toBe("claimable — waiting for the next worker poll");
  });

  it("names the worker an apply is reserved for", () => {
    const r = row({ state: "confirmed", blocking_reason: "apply_affinity_hold" }, { worker: vm });
    expect(blockerOf(r, [r], 1).text).toBe("apply affinity reservation (worker vm)");
  });

  it("never shows a bare unknown code without words", () => {
    // The env's own lock, not the state lock (SPECS §11.2) — the two are not conflated.
    expect(blockerOf(row({ blocking_reason: "env_locked" }), [], 1).text).toBe("the environment is locked");
  });

  it("says a run is backing off after transient failures, with the time left and a glyph", () => {
    const now = new Date("2026-10-10T20:00:00Z");
    const r = row({
      blocking_reason: "claim_backoff",
      claimable_after: "2026-10-10T20:00:38Z",
      claim_attempts: 3,
    });
    const b = blockerOf(r, [r], 1, now);
    expect(b.text).toBe("backing off after 3 transient failures preparing its job — next try in 38s");
    expect(b.icon).toBeDefined();
    const due = row({
      blocking_reason: "claim_backoff",
      claimable_after: "2026-10-10T19:59:59Z",
      claim_attempts: 1,
    });
    expect(blockerOf(due, [due], 1, now).text).toBe(
      "backing off after 1 transient failure preparing its job — next try at the next worker poll",
    );
  });

  it("filters on stack/env, worker and pool, every term must match", () => {
    const r = row(
      {},
      { stack: { id: "s", name: "demo-network" } as QueueRow["stack"], env: makeEnv(), worker: vm },
    );
    expect(matches(r, "")).toBe(true);
    expect(matches(r, "demo-network/prod")).toBe(true);
    expect(matches(r, "network default")).toBe(true);
    expect(matches(r, "network staging")).toBe(false);
  });

  it("measures the wait from the run's creation", () => {
    const r = row({}, { run: makeRun({ created_at: "2026-10-10T20:00:00Z" }) });
    expect(waitedSeconds(r, new Date("2026-10-10T20:03:00Z"))).toBe(180);
    expect(waitedSeconds(row({}), new Date())).toBeNull();
  });
});

describe("queue model — holder awaiting a person", () => {
  it("says the wait is on a decision when an unconfirmed plan holds the env", () => {
    const holder = row({ run_id: "01a1276cffff", state: "unconfirmed", worker_id: "w1" });
    const blocked = row({ run_id: "01a1276d", blocking_reason: "active_run" });
    expect(blockerOf(blocked, [holder, blocked], 1).text).toBe(
      "a plan on this environment awaits a human decision",
    );
  });
});
