import { useState } from "react";
import { Button } from "@/components/ui";

interface Step {
  eyebrow: string;
  title: string;
  body: string;
}

// First-login tour: explains the model in a few calm steps (no gamification — DESIGN §1).
const STEPS: Step[] = [
  {
    eyebrow: "welcome",
    title: "A control room for your infrastructure",
    body: "Stackd orchestrates plan → human confirmation → apply on self-hosted workers. The API is the single source of truth; every change is an auditable event.",
  },
  {
    eyebrow: "stacks / environments",
    title: "Template vs instance",
    body: "A stack is a repo + folder (the template). An environment (dev, staging, prod) is an instance with its own state, variables and protections. A run always belongs to an environment.",
  },
  {
    eyebrow: "variables",
    title: "Configuration in layers",
    body: "Variables resolve weakest → strongest: variable sets < stack < environment. Each resolved value shows its provenance, so you always know where it came from — and whether it was overridden.",
  },
  {
    eyebrow: "runs",
    title: "Plan, then a human confirms",
    body: "A run moves through a visible state machine (the phase rail). A non-empty plan waits at unconfirmed — the amber state — until someone confirms the apply. Amber always means “your call”.",
  },
  {
    eyebrow: "permissions",
    title: "Tiers, 4-eyes and destroy",
    body: "Who can apply depends on the environment tier and your ceiling. On prod, the person who triggered a run can’t confirm it (4-eyes), and destroys need an explicit right.",
  },
  {
    eyebrow: "dependencies / health",
    title: "Cascades, mocks & observability",
    body: "Environments pass outputs to each other; mock values let you bootstrap before the upstream exists (but block apply). Workers & health shows live status and structured logs for debugging.",
  },
];

export function Walkthrough({ onDone }: { onDone: () => void }) {
  const [i, setI] = useState(0);
  const step = STEPS[i];
  const last = i === STEPS.length - 1;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Welcome to Stackd"
      className="fixed inset-0 z-50 flex items-center justify-center p-6"
      style={{ backgroundColor: "var(--color-overlay)" }}
    >
      <div
        className="w-full max-w-[520px] rounded-base p-7"
        style={{ backgroundColor: "var(--color-bg-surface)", border: "1px solid var(--color-border)" }}
      >
        <div className="font-data text-[12px] uppercase tracking-wide" style={{ color: "var(--color-accent)" }}>
          {step.eyebrow}
        </div>
        <h2 className="mt-2 text-[20px] font-semibold tracking-[-0.01em]">{step.title}</h2>
        <p className="mt-3 text-[14px]" style={{ color: "var(--color-text-secondary)", lineHeight: 1.6 }}>
          {step.body}
        </p>

        <div className="mt-6 flex items-center justify-between">
          <div className="flex items-center gap-1.5" aria-hidden>
            {STEPS.map((_, idx) => (
              <span
                key={idx}
                style={{
                  width: idx === i ? 18 : 6,
                  height: 6,
                  borderRadius: 3,
                  backgroundColor: idx === i ? "var(--color-accent)" : "var(--color-border)",
                }}
              />
            ))}
          </div>
          <div className="flex items-center gap-2">
            {i > 0 && <Button onClick={() => setI(i - 1)}>Back</Button>}
            {!last ? (
              <Button variant="accent" onClick={() => setI(i + 1)}>
                Next
              </Button>
            ) : (
              <Button variant="accent" onClick={onDone}>
                Get started
              </Button>
            )}
          </div>
        </div>

        <button
          type="button"
          onClick={onDone}
          className="mt-4 text-[12px]"
          style={{ color: "var(--color-text-secondary)" }}
        >
          Skip
        </button>
      </div>
    </div>
  );
}
