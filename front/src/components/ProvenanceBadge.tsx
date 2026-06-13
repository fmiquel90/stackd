// Variable provenance (SPECS §3.4 / DESIGN §5.2): where a resolved variable came from.
export type Provenance =
  | { kind: "set"; name: string }
  | { kind: "stack" }
  | { kind: "env" }
  | { kind: "dependency"; name: string }
  | { kind: "mock" };

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
  }
}

/** Parse the API provenance string ("set:common-aws" | "stack" | "env" | "dependency:x" | "mock"). */
export function parseProvenance(s: string): Provenance {
  if (s.startsWith("set:")) return { kind: "set", name: s.slice(4) };
  if (s.startsWith("dependency:")) return { kind: "dependency", name: s.slice(11) };
  if (s === "mock") return { kind: "mock" };
  if (s === "env") return { kind: "env" };
  return { kind: "stack" };
}

export function ProvenanceBadge({ provenance }: { provenance: Provenance }) {
  const isMock = provenance.kind === "mock";
  const color = isMock ? "var(--color-mock)" : "var(--color-text-secondary)";
  return (
    <span
      className="font-data rounded-badge px-1.5 py-0.5 text-[12px]"
      style={{ color, border: `1px solid ${color}` }}
    >
      {label(provenance)}
    </span>
  );
}
