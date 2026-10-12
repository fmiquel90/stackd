import { ProvenanceBadge, type Provenance } from "./ProvenanceBadge";

export default { title: "Identity / ProvenanceBadge" };

const ALL: Provenance[] = [
  { kind: "set", name: "common-aws" },
  { kind: "stack" },
  { kind: "env" },
  { kind: "dependency", name: "demo-network/prod" },
  { kind: "mock" },
  { kind: "secret", name: "proton" },
  { kind: "secret_fallback", name: "proton" },
  { kind: "secret_override", name: "proton" },
];

export const EveryKind = () => (
  <div className="flex flex-col items-start gap-2">
    {ALL.map((p, i) => (
      <ProvenanceBadge key={i} provenance={p} />
    ))}
  </div>
);
