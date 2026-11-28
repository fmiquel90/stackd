import { describe, expect, it, vi } from "vitest";
import type { LogChunk } from "@/api/types";
import { LogModelBuilder, buildLogModel, mergeChunks } from "./logModel";
import { clockSeconds } from "./time";

vi.mock("./time", async (original) => {
  const real = await original<typeof import("./time")>();
  return { ...real, clockSeconds: vi.fn(real.clockSeconds) };
});

const chunk = (phase: string, seq: number, section: string | null, n: number, error = false): LogChunk => ({
  phase,
  seq,
  section,
  lines: Array.from({ length: n }, (_, i) => ({
    t: `20:${String(seq % 60).padStart(2, "0")}:${String(i % 60).padStart(2, "0")}`,
    msg: error && i === 0 ? "│ Error: boom" : `${phase} ${seq}.${i}`,
  })),
});

describe("LogModelBuilder — incremental", () => {
  it("processes only the appended chunk's lines", () => {
    const parse = vi.mocked(clockSeconds);
    const b = new LogModelBuilder();
    let chunks: LogChunk[] = [];
    for (let seq = 0; seq < 50; seq++) chunks = mergeChunks(chunks, [chunk("applying", seq, null, 200)]);
    parse.mockClear();
    b.update(chunks);
    expect(parse).toHaveBeenCalledTimes(10_000);

    parse.mockClear();
    const m = b.update(mergeChunks(chunks, [chunk("applying", 50, null, 3)]));
    expect(parse).toHaveBeenCalledTimes(3);
    expect(m.lines).toHaveLength(10_003);
    expect(m.lines.at(-1)?.id).toBe("applying.10003");
  });

  it("returns the same model for the same list, and a new one per append", () => {
    const b = new LogModelBuilder();
    const chunks = [chunk("planning", 0, null, 2)];
    const m1 = b.update(chunks);
    expect(b.update(chunks)).toBe(m1);
    const m2 = b.update(mergeChunks(chunks, [chunk("planning", 1, null, 1)]));
    expect(m2).not.toBe(m1);
    expect(m2.rows).not.toBe(m1.rows);
  });

  it("matches a full build whatever the arrival order", () => {
    const all = [
      chunk("planning", 0, null, 3),
      chunk("planning", 1, null, 2, true),
      chunk("planning", 2, "plan", 4),
      chunk("checking", 0, "hook:policy", 2),
      chunk("applying", 0, null, 5),
      chunk("applying", 1, null, 1, true),
    ];
    // In order, one at a time; out of order (a late planning chunk after checking); batched.
    const orders = [
      [[0], [1], [2], [3], [4], [5]],
      [[3, 0], [2], [1], [5, 4]],
      [[0, 1], [3], [2], [4], [5]],
    ];
    for (const order of orders) {
      const b = new LogModelBuilder();
      let chunks: LogChunk[] = [];
      for (const batch of order) {
        chunks = mergeChunks(
          chunks,
          batch.map((i) => all[i]),
        );
        expect(b.update(chunks)).toEqual(buildLogModel(chunks));
      }
    }
  });

  it("leaves earlier models untouched", () => {
    const b = new LogModelBuilder();
    const first = [chunk("applying", 0, null, 2)];
    const m1 = b.update(first);
    const frozen = structuredClone(m1);
    b.update(mergeChunks(first, [chunk("applying", 1, null, 2, true)]));
    expect(m1).toEqual(frozen);
  });

  it("keeps the plan's identity while other lines stream in", () => {
    const b = new LogModelBuilder();
    let chunks = [chunk("planning", 0, "plan", 3)];
    const plan = b.update(chunks).plan;
    chunks = mergeChunks(chunks, [chunk("applying", 0, null, 100)]);
    expect(b.update(chunks).plan).toBe(plan);
    chunks = mergeChunks(chunks, [chunk("planning", 1, "plan", 1)]);
    const after = b.update(chunks).plan;
    expect(after).not.toBe(plan);
    expect(after).toHaveLength(4);
  });

  it("rebuilds when the list is replaced (a full refetch)", () => {
    const b = new LogModelBuilder();
    b.update([chunk("planning", 0, null, 2), chunk("planning", 1, null, 2)]);
    const refetched = [chunk("planning", 0, null, 2)];
    expect(b.update(refetched)).toEqual(buildLogModel(refetched));
  });
});

describe("mergeChunks", () => {
  // A chunk whose `phase` reads are counted: merging must not re-key the chunks already merged.
  const counted = (phase: string, seq: number, reads: { n: number }): LogChunk => ({
    get phase() {
      reads.n++;
      return phase;
    },
    seq,
    section: null,
    lines: [],
  });

  it("does not re-key the existing chunks on each merge", () => {
    const reads = { n: 0 };
    let list = mergeChunks(
      [],
      Array.from({ length: 100 }, (_, i) => counted("applying", i, reads)),
    );
    reads.n = 0;
    list = mergeChunks(list, [counted("applying", 100, reads)]);
    list = mergeChunks(list, [counted("applying", 101, reads)]);
    expect(reads.n).toBeLessThanOrEqual(4);
    expect(list).toHaveLength(102);
  });

  it("de-duplicates by (phase, seq), also against a list it did not build", () => {
    const base = [chunk("planning", 0, null, 1)];
    const a = mergeChunks(base, [chunk("planning", 1, null, 1), chunk("planning", 0, null, 1)]);
    expect(a.map((c) => c.seq)).toEqual([0, 1]);
    expect(mergeChunks(a, [chunk("planning", 1, null, 1)])).toBe(a);
    // Merging into the older list again must not see the keys handed over to `a`.
    expect(mergeChunks(base, [chunk("planning", 1, null, 1)]).map((c) => c.seq)).toEqual([0, 1]);
  });
});
