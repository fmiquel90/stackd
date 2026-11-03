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
    expect(blockerOf(row({ blocking_reason: "env_locked" }), [], 1).text).toBe(
      "the environment's state is locked",
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
