import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { Link, MemoryRouter, Route, Routes } from "react-router-dom";
import { ErrorBoundary, RouteErrorBoundary } from "./ErrorBoundary";

let broken = true;
function Fragile() {
  if (broken) throw new Error("tier 'prod' is not in the catalog");
  return <p>rendered</p>;
}

describe("ErrorBoundary", () => {
  // React reports caught render errors to the console; keep the test output readable.
  beforeEach(() => {
    broken = true;
    vi.spyOn(console, "error").mockImplementation(() => {});
  });
  afterEach(() => vi.restoreAllMocks());

  it("renders the fallback in place of a throwing child, and its siblings survive", () => {
    render(
      <>
        <p>sibling</p>
        <ErrorBoundary what="The logs">
          <Fragile />
        </ErrorBoundary>
      </>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("The logs failed to display.");
    expect(screen.getByRole("alert")).toHaveTextContent("tier 'prod' is not in the catalog");
    expect(screen.getByText("sibling")).toBeInTheDocument();
  });

  it("retry renders the child again", () => {
    render(
      <ErrorBoundary what="The plan">
        <Fragile />
      </ErrorBoundary>,
    );
    broken = false;
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(screen.getByText("rendered")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("resets when its reset key changes", () => {
    const view = render(
      <ErrorBoundary what="The logs" resetKey="run-a">
        <Fragile />
      </ErrorBoundary>,
    );
    broken = false;
    view.rerender(
      <ErrorBoundary what="The logs" resetKey="run-a">
        <Fragile />
      </ErrorBoundary>,
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
    view.rerender(
      <ErrorBoundary what="The logs" resetKey="run-b">
        <Fragile />
      </ErrorBoundary>,
    );
    expect(screen.getByText("rendered")).toBeInTheDocument();
  });

  it("at the route level, navigating to another page clears the error", () => {
    render(
      <MemoryRouter initialEntries={["/broken"]}>
        <Link to="/fine">elsewhere</Link>
        <RouteErrorBoundary>
          <Routes>
            <Route path="/broken" element={<Fragile />} />
            <Route path="/fine" element={<p>fine page</p>} />
          </Routes>
        </RouteErrorBoundary>
      </MemoryRouter>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("This page failed to display.");
    fireEvent.click(screen.getByText("elsewhere"));
    expect(screen.getByText("fine page")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
});
