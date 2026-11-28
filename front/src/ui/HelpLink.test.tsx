import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { HelpLink } from "./primitives";

describe("HelpLink", () => {
  it("is a named link into the guide that opens in a new tab and says so", () => {
    render(<HelpLink topic="fourEyes">How four-eyes works</HelpLink>);
    const link = screen.getByRole("link", { name: /^How four-eyes works ?\(opens in a new tab\)$/ });
    expect(link.getAttribute("href")).toMatch(/guide\/runs-and-approvals\/#four-eyes$/);
    expect(link.getAttribute("target")).toBe("_blank");
    expect(link.getAttribute("rel")).toBe("noopener noreferrer");
  });
});
