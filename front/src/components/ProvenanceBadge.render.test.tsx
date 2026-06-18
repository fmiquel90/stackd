import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { ProvenanceBadge, parseProvenance } from "./ProvenanceBadge";

describe("ProvenanceBadge", () => {
  it("renders the human label for a parsed provenance", () => {
    render(<ProvenanceBadge provenance={parseProvenance("set:common-aws")} />);
    expect(screen.getByText("set:common-aws")).toBeInTheDocument();
  });

  it("renders MOCK for a mock-sourced value", () => {
    render(<ProvenanceBadge provenance={{ kind: "mock" }} />);
    expect(screen.getByText("MOCK")).toBeInTheDocument();
  });
});
