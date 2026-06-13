import { PhaseRail, type Phase } from "./PhaseRail";

export default { title: "Identity / PhaseRail" };

const PHASES: Phase[] = [
  { key: "preparing", label: "Preparing", status: "done", meta: "12s · clone, init" },
  { key: "planning", label: "Planning", status: "done", meta: "8s · +3 ~1 −0" },
  { key: "checking", label: "Checking", status: "active", meta: "2 hooks" },
  { key: "apply", label: "Apply", status: "waiting", meta: "awaiting confirmation" },
  { key: "finished", label: "Done", status: "pending" },
];

export const Vertical = () => (
  <div style={{ width: 220 }}>
    <PhaseRail phases={PHASES} />
  </div>
);

export const Failed = () => (
  <div style={{ width: 220 }}>
    <PhaseRail
      phases={[
        { key: "preparing", label: "Preparing", status: "done" },
        { key: "planning", label: "Planning", status: "failed", meta: "exit 1" },
        { key: "apply", label: "Apply", status: "pending" },
      ]}
    />
  </div>
);

export const Mini = () => <PhaseRail variant="mini" phases={PHASES} />;
