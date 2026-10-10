import { Badge } from "@/ui/primitives";

// Variable provenance (SPECS §3.4, §15.2 / DESIGN §5.2): where a resolved variable came from. A mock
// is "not real" (magenta); a fallback or a break-glass override is not the real secret and gates the
// apply (amber, SPECS §15.5). Everything else is neutral.

export type Provenance =
  | { kind: "set"; name: string }
  | { kind: "stack" }
  | { kind: "env" }
  | { kind: "dependency"; name: string }
  | { kind: "mock" }
  | { kind: "secret"; name: string }
  | { kind: "secret_fallback"; name: string }
  | { kind: "secret_override"; name: string };

/** Parse the API provenance string. Longer prefixes first (`secret_fallback:` before `secret:`). */
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

export function provenanceLabel(p: Provenance): string {
  switch (p.kind) {
    case "set":
      return `set:${p.name}`;
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
    default:
      return p.kind;
  }
}

/** True for values that are not the real thing (listed first on the Inputs tab). */
export function isNotReal(p: Provenance): boolean {
  return p.kind === "mock" || p.kind === "secret_fallback" || p.kind === "secret_override";
}

export function ProvenanceBadge({ provenance }: { provenance: Provenance }) {
  const tone =
    provenance.kind === "mock"
      ? "mock"
      : provenance.kind === "secret_fallback" || provenance.kind === "secret_override"
        ? "decision"
        : "neutral";
  const glyph = provenance.kind === "mock" ? "◆" : tone === "decision" ? "◈" : undefined;
  return (
    <Badge tone={tone} glyph={glyph} className="normal-case tracking-normal">
      {provenanceLabel(provenance)}
    </Badge>
  );
}
