import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { ALICE, BOB, TIERS, makeEnv, makeRun } from "@/lib/__fixtures__/factories";
import { runRights, whoMayConfirm } from "@/lib/permissions";
import { RunActionBar } from "./RunActionBar";
import { RunStateBadge } from "./StateBadge";
import { TypeToConfirm } from "./TypeToConfirm";
import { TooltipProvider } from "@/ui/overlays";

const env = makeEnv();
const prod = TIERS[2];

function bar(me = ALICE, run = makeRun()) {
  const rights = runRights(run, env, prod, me);
  return render(
    <MemoryRouter>
      <TooltipProvider>
        <RunActionBar
          run={run}
          env={env}
          rights={rights}
          me={me}
          triggerer="bob@dev.local"
          whoMayConfirm={whoMayConfirm(run, env, rights, "bob@dev.local")}
          cascade={[]}
          destroyed={[]}
          replaced={[]}
          onConfirm={vi.fn()}
          onDiscard={vi.fn()}
          onCancel={vi.fn()}
          onReplan={vi.fn()}
        />
      </TooltipProvider>
    </MemoryRouter>,
  );
}

describe("StateBadge", () => {
  it("never says only 'finished': applied and no changes read differently (gap 4)", () => {
    const { rerender } = render(<RunStateBadge run={makeRun({ state: "finished", confirmed_at: "x" })} />);
    expect(screen.getByText("APPLIED")).toBeInTheDocument();
    rerender(
      <RunStateBadge run={makeRun({ state: "finished", plan_summary: { add: 0, change: 0, destroy: 0 } })} />,
    );
    expect(screen.getByText("NO CHANGES")).toBeInTheDocument();
  });
  it("says where a run failed", () => {
    render(
      <RunStateBadge
        run={makeRun({
          state: "failed",
          check_results: { results: [{ name: "p", status: "fail", stage: "after_plan" }] },
        })}
      />,
    );
    expect(screen.getByText("FAILED at Check")).toBeInTheDocument();
  });
  it("labels the personal decision signal in text", () => {
    render(<RunStateBadge run={makeRun()} decision="needsYou" />);
    expect(screen.getByText("· NEEDS YOU")).toBeInTheDocument();
  });
});

describe("RunActionBar", () => {
  it("shows bob why he cannot confirm, in visible text", () => {
    bar(BOB);
    const confirm = screen.getByRole("button", { name: /Confirm apply/ });
    expect(confirm).toBeDisabled();
    expect(screen.getByText(/approver role required; you triggered this run/)).toBeVisible();
    expect(confirm).toHaveAttribute("aria-describedby");
    expect(screen.getByText(/needs an approver with tier prod other than bob@dev.local/)).toBeInTheDocument();
  });
  it("tells alice the decision is hers, with the four-eyes context", () => {
    bar(ALICE);
    expect(screen.getByRole("button", { name: /Confirm apply/ })).toBeEnabled();
    expect(screen.getByText("Waiting for your decision.")).toBeInTheDocument();
    expect(
      screen.getByText(/bob@dev.local triggered this run — someone else must confirm it/),
    ).toBeInTheDocument();
  });
  it("does not offer confirm or discard on a finished run", () => {
    bar(ALICE, makeRun({ state: "finished", confirmed_at: "x", confirmed_by_user_id: "user-alice" }));
    expect(screen.queryByRole("button", { name: /Confirm/ })).toBeNull();
    expect(screen.queryByRole("button", { name: /Discard/ })).toBeNull();
  });
});

describe("TypeToConfirm", () => {
  it("enables the submit only on the exact env name, and lists every destroyed address", () => {
    const onConfirm = vi.fn();
    render(
      <TypeToConfirm
        name="prod"
        verb="destroy"
        because={["destroy run"]}
        counts={{ add: 0, change: 0, destroy: 2 }}
        destroyed={["a.one", "a.two"]}
        replaced={[]}
        cascade={[]}
        onConfirm={onConfirm}
        onCancel={vi.fn()}
      />,
    );
    expect(screen.getByText("a.one")).toBeInTheDocument();
    expect(screen.getByText("a.two")).toBeInTheDocument();
    const submit = screen.getByRole("button", { name: "Confirm destroy on prod" });
    expect(submit).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/to confirm/), { target: { value: "Prod" } });
    expect(submit).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/to confirm/), { target: { value: "prod" } });
    fireEvent.click(submit);
    expect(onConfirm).toHaveBeenCalledOnce();
  });
});
