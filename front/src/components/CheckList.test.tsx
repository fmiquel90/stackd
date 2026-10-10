import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import type { Run } from "@/api/types";
import { CheckList, checkEntries, ranAfterPlanHooks } from "./CheckList";

type CheckResults = Run["check_results"];

// One check list per stage, as the API stores it (SPECS §8.3).
const WITH_RESULTS: CheckResults = {
  results: [
    { name: "fmt", status: "ok", detail: null, stage: "before_plan" },
    { name: "infracost", status: "warn", detail: "exit 1", stage: "after_plan" },
    { name: "smoke", status: "warn", detail: "timed out after 600s", stage: "after_apply" },
  ],
  checks: [
    { name: "infracost", status: "warn", detail: "exit 1", stage: "after_plan" },
    { name: "smoke", status: "warn", detail: "timed out after 600s", stage: "after_apply" },
  ],
};

const rowOf = (name: string) => screen.getByText(name).closest("tr") as HTMLElement;

afterEach(() => {
  vi.restoreAllMocks();
});

describe("CheckList", () => {
  it("shows the stage next to each check", () => {
    render(<CheckList checkResults={WITH_RESULTS} />);
    expect(within(rowOf("fmt")).getByText("before_plan")).toBeInTheDocument();
    expect(within(rowOf("infracost")).getByText("after_plan")).toBeInTheDocument();
    expect(within(rowOf("smoke")).getByText("after_apply")).toBeInTheDocument();
  });

  it("lists a passing check from `results` with an ok label and icon, not color alone", () => {
    render(<CheckList checkResults={WITH_RESULTS} />);
    const status = within(rowOf("fmt")).getByText("ok");
    expect(status.querySelector("svg")).not.toBeNull();
  });

  it("falls back to `checks` for an older run, without a stage", () => {
    render(
      <CheckList checkResults={{ checks: [{ name: "tfsec", status: "fail", detail: "2 high findings" }] }} />,
    );
    const row = rowOf("tfsec");
    expect(within(row).getByText("fail")).toBeInTheDocument();
    expect(within(row).getByText("2 high findings")).toBeInTheDocument();
    expect(within(row).getByTitle(/predates per-stage check results/)).toHaveTextContent("—");
  });

  it("keys rows per entry: one hook name at two stages renders twice, with no duplicate-key warning", () => {
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    render(
      <CheckList
        checkResults={{
          results: [
            { name: "policy", status: "warn", detail: "exit 1", stage: "after_plan" },
            { name: "policy", status: "warn", detail: "exit 1", stage: "after_apply" },
          ],
        }}
      />,
    );
    const rows = screen.getAllByText("policy").map((el) => el.closest("tr") as HTMLElement);
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByText("after_plan")).toBeInTheDocument();
    expect(within(rows[1]).getByText("after_apply")).toBeInTheDocument();
    const keyWarnings = errors.mock.calls.filter((args) =>
      args.some((a) => typeof a === "string" && a.includes("same key")),
    );
    expect(keyWarnings).toEqual([]);
  });

  it("notes that a plan-stage warn needs confirmation and an apply-stage warn only informs", () => {
    render(<CheckList checkResults={WITH_RESULTS} />);
    expect(within(rowOf("infracost")).getByText(/manual confirmation required/)).toBeInTheDocument();
    expect(within(rowOf("smoke")).getByText(/after confirmation — informational/)).toBeInTheDocument();
  });

  it("renders nothing when the run has no check", () => {
    const { container } = render(<CheckList checkResults={null} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("checkEntries", () => {
  it("prefers `results` and falls back to `checks`", () => {
    expect(checkEntries(WITH_RESULTS)).toHaveLength(3);
    expect(checkEntries({ checks: [{ name: "a", status: "warn" }] })).toHaveLength(1);
    expect(checkEntries(null)).toEqual([]);
  });
});

describe("ranAfterPlanHooks (the rail's Checking segment)", () => {
  it("is true when any result ran at after_plan, even an ok one", () => {
    expect(ranAfterPlanHooks({ results: [{ name: "tfsec", status: "ok", stage: "after_plan" }] })).toBe(true);
  });

  it("is false when no hook ran at after_plan, even with warnings at other stages", () => {
    expect(
      ranAfterPlanHooks({
        results: [
          { name: "fmt", status: "ok", stage: "before_plan" },
          { name: "secret_leak_suspected", status: "warn", stage: "plan" },
          { name: "smoke", status: "warn", stage: "after_apply" },
        ],
      }),
    ).toBe(false);
    expect(ranAfterPlanHooks(null)).toBe(false);
  });

  it("keeps the previous rule for an older run whose checks carry no stage", () => {
    expect(ranAfterPlanHooks({ checks: [{ name: "infracost", status: "warn" }] })).toBe(true);
    expect(ranAfterPlanHooks({ checks: [] })).toBe(false);
  });
});
