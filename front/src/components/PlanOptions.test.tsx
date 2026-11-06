import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { buildCommandArgs } from "./CommandPanel";
import { PlanOptionsChips, parseAddresses, toPlanOptions } from "./PlanOptions";

describe("plan options (SPECS §3.5)", () => {
  it("parses one address per line, trimmed, blank lines and duplicates dropped", () => {
    expect(parseAddresses(' aws_s3_bucket.a \n\nmodule.m["k"].x.y[0]\naws_s3_bucket.a\n')).toEqual([
      "aws_s3_bucket.a",
      'module.m["k"].x.y[0]',
    ]);
  });

  it("is null for a full plan, and keeps only the non-empty lists", () => {
    expect(toPlanOptions("", "  \n")).toBeNull();
    expect(toPlanOptions("a.b", "")).toEqual({ targets: ["a.b"] });
    expect(toPlanOptions("", "c.d")).toEqual({ replace: ["c.d"] });
  });

  it("shows a labelled chip with the addresses, never colour alone", () => {
    render(<PlanOptionsChips options={{ targets: ["aws_s3_bucket.a"], replace: ["aws_instance.web"] }} />);
    expect(screen.getByText("Targeted")).toBeInTheDocument();
    expect(screen.getByText("Replace")).toBeInTheDocument();
    expect(screen.getByText("aws_s3_bucket.a")).toBeInTheDocument();
    expect(screen.getByText("aws_instance.web")).toBeInTheDocument();
  });

  it("renders nothing for a full plan", () => {
    const { container } = render(<PlanOptionsChips options={null} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("command options (SPECS §4.3)", () => {
  it("puts the allowlisted options first", () => {
    expect(buildCommandArgs("state rm", "aws_s3_bucket.a", { dryRun: true })).toEqual([
      "-dry-run",
      "aws_s3_bucket.a",
    ]);
    expect(buildCommandArgs("refresh", "", { targets: "a.b\nc.d\na.b" })).toEqual([
      "-target=a.b",
      "-target=c.d",
    ]);
  });

  it("never adds an option the command does not allow", () => {
    expect(buildCommandArgs("output", "x", { dryRun: true, targets: "a.b" })).toEqual(["x"]);
    expect(buildCommandArgs("force-unlock", "abc-123", { dryRun: true })).toEqual(["abc-123"]);
  });
});
