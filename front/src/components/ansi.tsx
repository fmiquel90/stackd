import { Fragment, type CSSProperties } from "react";

// Minimal ANSI SGR renderer for tofu/terraform log lines (DESIGN: ANSI logs). No dependency:
// we only need the SGR subset tofu emits — reset, bold, and the 8+8 foreground colors — mapped
// onto design tokens so the palette stays consistent with the rest of the UI (no hard-coded color).

const FG: Record<number, string> = {
  30: "var(--color-text-secondary)",
  31: "var(--color-state-failed)",
  32: "var(--color-state-finished)",
  33: "var(--color-state-unconfirmed)",
  34: "var(--color-state-running)",
  35: "var(--color-mock)",
  36: "var(--color-accent)",
  37: "var(--color-text-primary)",
  90: "var(--color-text-secondary)",
  91: "var(--color-state-failed)",
  92: "var(--color-state-finished)",
  93: "var(--color-state-unconfirmed)",
  94: "var(--color-state-running)",
  95: "var(--color-mock)",
  96: "var(--color-accent)",
  97: "var(--color-text-primary)",
};

interface Style {
  color?: string;
  bold?: boolean;
}

function applyCodes(style: Style, codes: number[]): Style {
  let next = { ...style };
  // An empty parameter list (ESC[m) means reset, same as 0.
  for (const code of codes.length ? codes : [0]) {
    if (code === 0) next = {};
    else if (code === 1) next.bold = true;
    else if (code === 22) next.bold = false;
    else if (code === 39) next.color = undefined;
    else if (code in FG) next.color = FG[code];
    // other codes (background, underline, 256/truecolor) are ignored, not rendered as text
  }
  return next;
}

// Split on SGR sequences; drop any other escape sequence (cursor moves, etc.) and carriage returns.
const SGR = /\x1b\[([0-9;]*)m/g;
const OTHER_ESC = /\x1b\[[0-9;?]*[A-Za-z]|\r/g;

export function AnsiText({ text }: { text: string }) {
  const segments: { text: string; style: Style }[] = [];
  let style: Style = {};
  let lastIndex = 0;
  let m: RegExpExecArray | null;
  SGR.lastIndex = 0;
  while ((m = SGR.exec(text)) !== null) {
    if (m.index > lastIndex) segments.push({ text: text.slice(lastIndex, m.index), style });
    const codes = m[1] ? m[1].split(";").map(Number) : [];
    style = applyCodes(style, codes);
    lastIndex = SGR.lastIndex;
  }
  if (lastIndex < text.length) segments.push({ text: text.slice(lastIndex), style });

  return (
    <>
      {segments.map((seg, i) => {
        const clean = seg.text.replace(OTHER_ESC, "");
        if (!clean) return null;
        const css: CSSProperties = {};
        if (seg.style.color) css.color = seg.style.color;
        if (seg.style.bold) css.fontWeight = 600;
        return (
          <Fragment key={i}>
            {Object.keys(css).length ? <span style={css}>{clean}</span> : clean}
          </Fragment>
        );
      })}
    </>
  );
}
