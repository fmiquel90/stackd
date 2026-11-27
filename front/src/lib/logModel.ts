import type { LogChunk } from "@/api/types";
import { clockSeconds } from "./time";

// Log model for the LogViewer (DESIGN §5.3): order, sections, line ids, first error, durations.
// The API orders chunks by (phase string, seq), so "checking" sorts before "planning" (AUDIT §6);
// we order by the lifecycle instead — phases of one run never interleave — then by seq.
// Built incrementally: a live apply streams tens of thousands of lines, one chunk per log signal, so
// each update processes only the new chunks (a full rebuild only when a chunk arrives out of order).

export const PHASE_ORDER = ["preparing", "planning", "checking", "applying", "running"];

export interface LogLine {
  /** Stable id `phase.n` (1-based within the phase) — the `?line=` anchor. */
  id: string;
  phase: string;
  section: string | null;
  sectionIndex: number;
  /** Line number within its phase (gutter). */
  n: number;
  t: string;
  msg: string;
  isError: boolean;
  /** Comment anchor coordinates (SPECS §16.2: phase + chunk seq + index in the chunk). */
  seq: number;
  idx: number;
}

export interface LogSection {
  index: number;
  phase: string;
  section: string | null;
  title: string;
  /** Index of the section's first line in `lines`. */
  start: number;
  count: number;
  durationSeconds: number | null;
  hasError: boolean;
}

/** A row of the viewer, every section expanded: section `i`'s header is at `start + i`. */
export type LogRow = { kind: "section"; index: number } | { kind: "line"; line: LogLine };

export interface LogModel {
  lines: LogLine[];
  sections: LogSection[];
  rows: LogRow[];
  /** Index in `lines` of the first error line, or -1. */
  firstError: number;
  errorCount: number;
  /** Highest seq seen per phase — the `after_seq` cursor for incremental fetches (SPECS §5.3). */
  cursor: Record<string, number>;
  durations: Record<string, number>;
  /** Lines of the `plan` section, for the PlanDiff parser — the same array until a plan line arrives. */
  plan: string[];
}

const ESC = String.fromCharCode(27);
const ERROR_LINE = new RegExp(`^\\s*(?:│\\s*)?(?:${ESC}\\[[0-9;]*m)*Error:`);

export function sectionTitle(phase: string, section: string | null): string {
  if (section === "plan") return "Plan output";
  if (section?.startsWith("hook:")) return `Hook · ${section.slice(5)}`;
  if (section) return section;
  const name: Record<string, string> = {
    planning: "Init & plan",
    checking: "Checks",
    applying: "Apply",
    running: "Command",
    preparing: "Prepare",
  };
  return name[phase] ?? phase;
}

function phaseRank(phase: string): number {
  const i = PHASE_ORDER.indexOf(phase);
  return i === -1 ? PHASE_ORDER.length : i;
}

const byLifecycle = (a: LogChunk, b: LogChunk) => phaseRank(a.phase) - phaseRank(b.phase) || a.seq - b.seq;

const chunkKey = (c: LogChunk) => `${c.phase}:${c.seq}`;
// The (phase, seq) keys of a merged list, handed over to the list that extends it.
const mergedKeys = new WeakMap<LogChunk[], Set<string>>();

/** Merge chunks into an existing list (incremental fetch), de-duplicated by (phase, seq). */
export function mergeChunks(existing: LogChunk[], incoming: LogChunk[]): LogChunk[] {
  const seen = mergedKeys.get(existing) ?? new Set(existing.map(chunkKey));
  const added = incoming.filter((c) => !seen.has(chunkKey(c)));
  if (!added.length) {
    mergedKeys.set(existing, seen);
    return existing;
  }
  const merged = [...existing, ...added];
  for (const c of added) seen.add(chunkKey(c));
  // The set now describes `merged`: a later merge into `existing` itself must rebuild its own.
  mergedKeys.delete(existing);
  mergedKeys.set(merged, seen);
  return merged;
}

const span = (b?: [number, number]) => (b ? (b[1] - b[0] + 86400) % 86400 : null);

function emptyState() {
  return {
    /** The input list the model reflects, and its last chunk in display order. */
    consumed: [] as LogChunk[],
    last: null as LogChunk | null,
    lines: [] as LogLine[],
    rows: [] as LogRow[],
    sections: [] as LogSection[],
    plan: [] as string[],
    perPhase: {} as Record<string, number>,
    cursor: {} as Record<string, number>,
    sectionBounds: [] as ([number, number] | undefined)[],
    phaseBounds: {} as Record<string, [number, number]>,
    firstError: -1,
    errorCount: 0,
    model: null as LogModel | null,
  };
}

/**
 * Keeps a log model in step with a chunk list that grows by appending (`mergeChunks`): `update`
 * processes only the chunks added since the previous call. Any other change to the list, or a new
 * chunk ordered before one already shown, rebuilds from scratch. Models are immutable snapshots.
 */
export class LogModelBuilder {
  private s = emptyState();

  update(chunks: LogChunk[]): LogModel {
    const s = this.s;
    if (s.model && chunks === s.consumed) return s.model;
    const extended = chunks.length >= s.consumed.length && s.consumed.every((c, i) => chunks[i] === c);
    let fresh = extended ? chunks.slice(s.consumed.length).sort(byLifecycle) : null;
    if (!fresh || (s.last && fresh.length && byLifecycle(fresh[0], s.last) < 0)) {
      this.s = emptyState();
      fresh = [...chunks].sort(byLifecycle);
    }
    this.s.consumed = chunks;
    if (this.s.model && !fresh.length) return this.s.model;
    this.append(fresh);
    return (this.s.model = this.snapshot());
  }

  private append(chunks: LogChunk[]) {
    const s = this.s;
    // Sections from earlier snapshots are copied before they change: snapshots stay immutable.
    const touched = new Set<number>();
    const plan: string[] = [];
    for (const chunk of chunks) {
      s.cursor[chunk.phase] = Math.max(s.cursor[chunk.phase] ?? -1, chunk.seq);
      let current = s.sections[s.sections.length - 1];
      if (!current || current.phase !== chunk.phase || current.section !== chunk.section) {
        current = {
          index: s.sections.length,
          phase: chunk.phase,
          section: chunk.section,
          title: sectionTitle(chunk.phase, chunk.section),
          start: s.lines.length,
          count: 0,
          durationSeconds: null,
          hasError: false,
        };
        s.sections.push(current);
        s.rows.push({ kind: "section", index: current.index });
        touched.add(current.index);
      } else if (!touched.has(current.index)) {
        current = { ...current };
        s.sections[current.index] = current;
        touched.add(current.index);
      }
      chunk.lines.forEach((l, idx) => {
        const n = (s.perPhase[chunk.phase] = (s.perPhase[chunk.phase] ?? 0) + 1);
        const isError = ERROR_LINE.test(l.msg);
        if (isError) {
          current.hasError = true;
          if (s.firstError < 0) s.firstError = s.lines.length;
          s.errorCount++;
        }
        const line: LogLine = {
          id: `${chunk.phase}.${n}`,
          phase: chunk.phase,
          section: chunk.section,
          sectionIndex: current.index,
          n,
          t: l.t,
          msg: l.msg,
          isError,
          seq: chunk.seq,
          idx,
        };
        s.lines.push(line);
        s.rows.push({ kind: "line", line });
        if (chunk.section === "plan") plan.push(l.msg);
        current.count++;
        const sec = clockSeconds(l.t);
        if (sec !== null) {
          const b = s.sectionBounds[current.index];
          s.sectionBounds[current.index] = b ? [b[0], sec] : [sec, sec];
          const p = s.phaseBounds[chunk.phase];
          s.phaseBounds[chunk.phase] = p ? [p[0], sec] : [sec, sec];
        }
      });
      s.last = chunk;
    }
    for (const i of touched) s.sections[i].durationSeconds = span(s.sectionBounds[i]);
    if (plan.length) s.plan = [...s.plan, ...plan];
  }

  private snapshot(): LogModel {
    const s = this.s;
    const durations: Record<string, number> = {};
    for (const phase of Object.keys(s.perPhase)) {
      const d = span(s.phaseBounds[phase]);
      if (d !== null) durations[phase] = d;
    }
    return {
      lines: s.lines.slice(),
      sections: s.sections.slice(),
      rows: s.rows.slice(),
      firstError: s.firstError,
      errorCount: s.errorCount,
      cursor: { ...s.cursor },
      durations,
      plan: s.plan,
    };
  }
}

export function buildLogModel(chunks: LogChunk[]): LogModel {
  return new LogModelBuilder().update(chunks);
}
