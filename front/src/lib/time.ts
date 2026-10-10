// Time formatting (DESIGN §7): relative under 24 h, absolute local ISO beyond; always rendered mono.

const MINUTE = 60;
const HOUR = 3600;
const DAY = 86400;

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

/** `2026-10-10 20:06` in local time — never a locale format like `10/10/2026, 8:06 PM`. */
export function isoLocal(date: Date | string): string {
  const d = typeof date === "string" ? new Date(date) : date;
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** `now`, `45s ago`, `12m ago`, `3h ago`, then the absolute ISO form past 24 h. */
export function relative(date: Date | string, now: Date = new Date()): string {
  const d = typeof date === "string" ? new Date(date) : date;
  const s = Math.round((now.getTime() - d.getTime()) / 1000);
  if (s < 0) return isoLocal(d);
  if (s < 10) return "now";
  if (s < MINUTE) return `${s}s ago`;
  if (s < HOUR) return `${Math.floor(s / MINUTE)}m ago`;
  if (s < DAY) return `${Math.floor(s / HOUR)}h ago`;
  return isoLocal(d);
}

/** Compact age without "ago", for dense cells: `45s`, `12m`, `3h`, `4d`. */
export function age(date: Date | string, now: Date = new Date()): string {
  const d = typeof date === "string" ? new Date(date) : date;
  const s = Math.max(0, Math.round((now.getTime() - d.getTime()) / 1000));
  if (s < MINUTE) return `${s}s`;
  if (s < HOUR) return `${Math.floor(s / MINUTE)}m`;
  if (s < DAY) return `${Math.floor(s / HOUR)}h`;
  return `${Math.floor(s / DAY)}d`;
}

/** `8s`, `1m 05s`, `2h 03m`. */
export function formatDuration(seconds: number): string {
  const s = Math.max(0, Math.round(seconds));
  if (s < MINUTE) return `${s}s`;
  if (s < HOUR) return `${Math.floor(s / MINUTE)}m ${pad(s % MINUTE)}s`;
  return `${Math.floor(s / HOUR)}h ${pad(Math.floor((s % HOUR) / MINUTE))}m`;
}

/** Seconds since midnight of an `HH:MM:SS` log timestamp (the worker's line clock, SPECS §5.1). */
export function clockSeconds(t: string): number | null {
  const m = /^(\d{2}):(\d{2}):(\d{2})/.exec(t);
  return m ? Number(m[1]) * HOUR + Number(m[2]) * MINUTE + Number(m[3]) : null;
}
