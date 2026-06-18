import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { StateBadge } from "./StateBadge";

describe("StateBadge", () => {
  it("shows a label alongside the color so meaning never rides on color alone (DESIGN §7)", () => {
    render(<StateBadge state="finished" />);
    expect(screen.getByText("Finished")).toBeInTheDocument();
  });

  it("overlays the Mocked / Fallback modifiers when set", () => {
    const { rerender } = render(<StateBadge state="unconfirmed" mocked />);
    expect(screen.getByText("Mocked")).toBeInTheDocument();
    rerender(<StateBadge state="unconfirmed" fallback />);
    expect(screen.getByText("Fallback")).toBeInTheDocument();
  });

  it("shows no modifier by default", () => {
    render(<StateBadge state="planning" />);
    expect(screen.queryByText("Mocked")).not.toBeInTheDocument();
    expect(screen.queryByText("Fallback")).not.toBeInTheDocument();
  });
});
