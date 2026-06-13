import { ProvenanceBadge, type Provenance } from "./ProvenanceBadge";

export default { title: "Identity / ProvenanceBadge" };

const ALL: Provenance[] = [
  { kind: "set", name: "common-aws" },
  { kind: "stack" },
  { kind: "env" },
  { kind: "dependency", name: "core-network/prod" },
  { kind: "mock" },
];

export const AllKinds = () => (
  <div style={{ display: "flex", flexDirection: "column", gap: 8, alignItems: "flex-start" }}>
    {ALL.map((p, i) => (
      <ProvenanceBadge key={i} provenance={p} />
    ))}
  </div>
);
