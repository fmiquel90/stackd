import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

// Read from disk: Vitest stubs CSS imports (even `?raw`) to an empty string.
// (jsdom gives import.meta.url an http scheme, so resolve from the project root vitest runs in.)
const css = readFileSync(resolve(process.cwd(), "src/styles/tokens.css"), "utf8");

// Re-checks the contrast table of DESIGN §3.1 against the real token values, so a palette tweak that
// drops a pair under AA fails the build instead of shipping.

function block(selector: string): Record<string, string> {
  const start = css.indexOf(selector);
  const body = css.slice(css.indexOf("{", start) + 1, css.indexOf("}", start));
  const vars: Record<string, string> = {};
  for (const m of body.matchAll(/--color-([\w-]+):\s*(#[0-9a-fA-F]{6})/g)) vars[m[1]] = m[2];
  return vars;
}

function luminance(hex: string): number {
  const [r, g, b] = [1, 3, 5].map((i) => {
    const v = parseInt(hex.slice(i, i + 2), 16) / 255;
    return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a: string, b: string): number {
  const [x, y] = [luminance(a), luminance(b)].sort((p, q) => q - p);
  return (x + 0.05) / (y + 0.05);
}

const THEMES = { dark: block('[data-theme="dark"] {'), light: block('[data-theme="light"] {') };
const BACKGROUNDS = ["bg-base", "bg-surface", "bg-raised", "bg-sunken"];
const TEXT = [
  "text-primary",
  "text-secondary",
  "state-queued",
  "state-running",
  "state-decision",
  "state-finished",
  "state-failed",
  "mock",
];
const NON_TEXT = ["border-control", "focus"];
const TINTS = ["decision-tint", "failed-tint", "mock-tint"];
const FILLS = ["state-decision", "text-primary", "state-failed", "mock"];

describe.each(Object.entries(THEMES))("%s theme", (_name, t) => {
  it.each(TEXT.flatMap((fg) => BACKGROUNDS.map((bg) => [fg, bg])))("%s on %s ≥ 4.5", (fg, bg) => {
    expect(contrast(t[fg], t[bg])).toBeGreaterThanOrEqual(4.5);
  });

  it.each(NON_TEXT.flatMap((fg) => BACKGROUNDS.map((bg) => [fg, bg])))("%s on %s ≥ 3", (fg, bg) => {
    expect(contrast(t[fg], t[bg])).toBeGreaterThanOrEqual(3);
  });

  it.each(TEXT.flatMap((fg) => TINTS.map((bg) => [fg, bg])))("%s on %s ≥ 4.5", (fg, bg) => {
    expect(contrast(t[fg], t[bg])).toBeGreaterThanOrEqual(4.5);
  });

  it.each(FILLS)("on-fill on %s ≥ 4.5", (fill) => {
    expect(contrast(t["on-fill"], t[fill])).toBeGreaterThanOrEqual(4.5);
  });
});
