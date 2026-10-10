import type { LogChunk } from "@/api/types";
import { clockSeconds } from "./time";

// Log model for the LogViewer (DESIGN §5.3): order, sections, line ids, first error, durations.
// The API orders chunks by (phase string, seq), so "checking" sorts before "planning" (AUDIT §6);
// we order by the lifecycle instead — phases of one run never interleave — then by seq.

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

export interface LogModel {
  lines: LogLine[];
  sections: LogSection[];
  /** Index in `lines` of the first error line, or -1. */
  firstError: number;
  errorCount: number;
  /** Highest seq seen per phase — the `after_seq` cursor for incremental fetches (SPECS §5.3). */
  cursor: Record<string, number>;
  durations: Record<string, number>;
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

/** Merge chunks into an existing list (incremental fetch), de-duplicated by (phase, seq). */
export function mergeChunks(existing: LogChunk[], incoming: LogChunk[]): LogChunk[] {
  const seen = new Set(existing.map((c) => `${c.phase}:${c.seq}`));
  const added = incoming.filter((c) => !seen.has(`${c.phase}:${c.seq}`));
  return added.length ? [...existing, ...added] : existing;
}

export function buildLogModel(chunks: LogChunk[]): LogModel {
  const ordered = [...chunks].sort((a, b) => phaseRank(a.phase) - phaseRank(b.phase) || a.seq - b.seq);
  const lines: LogLine[] = [];
  const sections: LogSection[] = [];
  const perPhase: Record<string, number> = {};
  const cursor: Record<string, number> = {};
  const bounds: Record<string, [number, number]> = {};

  for (const chunk of ordered) {
    cursor[chunk.phase] = Math.max(cursor[chunk.phase] ?? -1, chunk.seq);
    let current = sections[sections.length - 1];
    if (!current || current.phase !== chunk.phase || current.section !== chunk.section) {
      current = {
        index: sections.length,
        phase: chunk.phase,
        section: chunk.section,
        title: sectionTitle(chunk.phase, chunk.section),
        start: lines.length,
        count: 0,
        durationSeconds: null,
        hasError: false,
      };
      sections.push(current);
    }
    chunk.lines.forEach((l, idx) => {
      const n = (perPhase[chunk.phase] = (perPhase[chunk.phase] ?? 0) + 1);
      const isError = ERROR_LINE.test(l.msg);
      if (isError) current.hasError = true;
      lines.push({
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
      });
      current.count++;
      const s = clockSeconds(l.t);
      if (s !== null) {
        const b = bounds[`${current.index}`];
        bounds[`${current.index}`] = b ? [b[0], s] : [s, s];
        const p = bounds[`phase:${chunk.phase}`];
        bounds[`phase:${chunk.phase}`] = p ? [p[0], s] : [s, s];
      }
    });
  }

  const span = (b?: [number, number]) => (b ? (b[1] - b[0] + 86400) % 86400 : null);
  for (const s of sections) s.durationSeconds = span(bounds[`${s.index}`]);
  const durations: Record<string, number> = {};
  for (const phase of Object.keys(perPhase)) {
    const d = span(bounds[`phase:${phase}`]);
    if (d !== null) durations[phase] = d;
  }

  const firstError = lines.findIndex((l) => l.isError);
  return {
    lines,
    sections,
    firstError,
    errorCount: lines.filter((l) => l.isError).length,
    cursor,
    durations,
  };
}

/** Lines of the `plan` section, for the PlanDiff parser. */
export function planLines(chunks: LogChunk[]): string[] {
  return [...chunks]
    .filter((c) => c.section === "plan")
    .sort((a, b) => phaseRank(a.phase) - phaseRank(b.phase) || a.seq - b.seq)
    .flatMap((c) => c.lines.map((l) => l.msg));
}
