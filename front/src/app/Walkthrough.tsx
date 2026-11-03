import { useState } from "react";
import { Button } from "@/ui/Button";
import { cn } from "@/ui/cn";
import { Dialog, DialogContent } from "@/ui/overlays";

// First-login tour (DESIGN §1: calm, no gamification). Closing it in any way — Skip, Esc, the close
// button — completes the onboarding, persisted server-side (no browser storage).

interface Step {
  eyebrow: string;
  title: string;
  body: string;
}

const STEPS: Step[] = [
  {
    eyebrow: "welcome",
    title: "A control room for your infrastructure",
    body: "Stackd runs plan → a human confirms → apply, on self-hosted workers. The API is the single source of truth; every change is an audited event.",
  },
  {
    eyebrow: "stacks and environments",
    title: "Template and instances",
    body: "A stack is a repository folder (the template). An environment — dev, staging, prod — is an instance with its own state, variables and protections. A run always belongs to one environment.",
  },
  {
    eyebrow: "runs",
    title: "Amber means a person must decide",
    body: "A plan with changes stops at UNCONFIRMED until someone who may confirm it does. Amber is reserved for that: “needs you” in the overview lists exactly the decisions you can take.",
  },
  {
    eyebrow: "permissions",
    title: "Tiers, four-eyes and destroy",
    body: "You may confirm on the tiers you are allowed. On four-eyes tiers and protected environments, whoever triggered a run cannot confirm it. Destroys need an explicit right and the environment name typed.",
  },
  {
    eyebrow: "dependencies",
    title: "Outputs flow; mocks are not real",
    body: "Environments pass outputs downstream. A mock value lets you bootstrap before the upstream exists — it shows in magenta, dashed, and blocks apply by default.",
  },
  {
    eyebrow: "getting around",
    title: "Keyboard first",
    body: "⌘K finds any stack, environment or run. Press ? for the shortcuts; single-key ones can be turned off in Settings.",
  },
];

export function Walkthrough({ onDone }: { onDone: () => void }) {
  const [i, setI] = useState(0);
  const step = STEPS[i];
  const last = i === STEPS.length - 1;
  return (
    <Dialog open onOpenChange={(open) => !open && onDone()}>
      <DialogContent title="Welcome to Stackd" className="max-w-[520px]">
        <div className="flex flex-col gap-3">
          <span className="eyebrow text-muted">
            {i + 1} / {STEPS.length} · {step.eyebrow}
          </span>
          <h2 className="text-title font-semibold">{step.title}</h2>
          <p className="text-muted">{step.body}</p>
          <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
            <div className="flex items-center gap-1.5" aria-hidden>
              {STEPS.map((s, idx) => (
                <span
                  key={s.eyebrow}
                  className={cn("h-1.5 rounded-full", idx === i ? "w-4 bg-ring" : "w-1.5 bg-line")}
                />
              ))}
            </div>
            <div className="flex items-center gap-2">
              <Button variant="ghost" onClick={onDone}>
                Skip
              </Button>
              {i > 0 && <Button onClick={() => setI(i - 1)}>Back</Button>}
              {last ? (
                <Button variant="primary" onClick={onDone}>
                  Get started
                </Button>
              ) : (
                <Button variant="primary" onClick={() => setI(i + 1)}>
                  Next
                </Button>
              )}
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
