import { memo } from "react";
import Anser from "anser";

// ANSI rendering with anser, mapped onto the tokens (DESIGN §5.3): red → failed, green → finished,
// yellow → bold primary (never amber: amber means a human decision), blue → running, magenta → mock.

const FG: Record<string, string> = {
  "ansi-black": "text-muted",
  "ansi-red": "text-st-failed",
  "ansi-green": "text-st-finished",
  "ansi-yellow": "text-primary font-semibold",
  "ansi-blue": "text-st-running",
  "ansi-magenta": "text-mocked",
  "ansi-cyan": "text-primary",
  "ansi-white": "text-primary",
  "ansi-bright-black": "text-muted",
  "ansi-bright-red": "text-st-failed",
  "ansi-bright-green": "text-st-finished",
  "ansi-bright-yellow": "text-primary font-semibold",
  "ansi-bright-blue": "text-st-running",
  "ansi-bright-magenta": "text-mocked",
  "ansi-bright-cyan": "text-primary",
  "ansi-bright-white": "text-primary",
};

/** Strip every escape sequence: for search and copy. */
export function stripAnsi(text: string): string {
  return Anser.ansiToText(text).replace(/\r/g, "");
}

export const AnsiText = memo(function AnsiText({ text }: { text: string }) {
  if (!text.includes(String.fromCharCode(27))) return <>{text.replace(/\r/g, "")}</>;
  const parts = Anser.ansiToJson(text, { use_classes: true, remove_empty: true });
  return (
    <>
      {parts.map((p, i) => {
        const cls = [p.fg ? FG[p.fg] : "", p.decorations?.includes("bold") ? "font-semibold" : ""]
          .filter(Boolean)
          .join(" ");
        const content = p.content.replace(/\r/g, "");
        return cls ? (
          <span key={i} className={cls}>
            {content}
          </span>
        ) : (
          <span key={i}>{content}</span>
        );
      })}
    </>
  );
});
