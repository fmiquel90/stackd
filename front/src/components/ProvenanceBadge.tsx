// Variable provenance (SPECS §3.4, §15.2 / DESIGN §5.2): where a resolved variable came from.
export type Provenance =
  | { kind: "set"; name: string }
  | { kind: "stack" }
  | { kind: "env" }
  | { kind: "dependency"; name: string }
  | { kind: "mock" }
  | { kind: "secret"; name: string } // live value from an external secret source
  | { kind: "secret_fallback"; name: string } // source down → static fallback used
  | { kind: "secret_override"; name: string }; // source down → break-glass override used

function label(p: Provenance): string {
  switch (p.kind) {
    case "set":
      return `set:${p.name}`;
    case "stack":
      return "stack";
    case "env":
      return "env";
    case "dependency":
      return `dependency:${p.name}`;
    case "mock":
      return "MOCK";
    case "secret":
      return `secret:${p.name}`;
    case "secret_fallback":
      return `FALLBACK:${p.name}`;
    case "secret_override":
      return `OVERRIDE:${p.name}`;
  }
}

/** Parse the API provenance string (§3.4, §15.2). */
export function parseProvenance(s: string): Provenance {
  if (s.startsWith("set:")) return { kind: "set", name: s.slice(4) };
  if (s.startsWith("dependency:")) return { kind: "dependency", name: s.slice(11) };
  if (s.startsWith("secret_fallback:")) return { kind: "secret_fallback", name: s.slice(16) };
  if (s.startsWith("secret_override:")) return { kind: "secret_override", name: s.slice(16) };
  if (s.startsWith("secret:")) return { kind: "secret", name: s.slice(7) };
  if (s === "mock") return { kind: "mock" };
  if (s === "env") return { kind: "env" };
  return { kind: "stack" };
}

// A value sourced from a fallback (not the real secret) is flagged in the warning hue, like mocks.
const FALLBACK_KINDS = new Set<Provenance["kind"]>(["secret_fallback", "secret_override"]);

function badgeColor(p: Provenance): string {
  if (p.kind === "mock") return "var(--color-mock)";
  if (FALLBACK_KINDS.has(p.kind)) return "var(--color-state-unconfirmed)";
  return "var(--color-text-secondary)";
}

export function ProvenanceBadge({ provenance }: { provenance: Provenance }) {
  const color = badgeColor(provenance);
  return (
    <span
      className="font-data rounded-badge px-1.5 py-0.5 text-[12px]"
      style={{ color, border: `1px solid ${color}` }}
    >
      {label(provenance)}
    </span>
  );
}
