import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import Anser from "anser";
import type { LogChunk } from "@/api/types";
import { mergeChunks } from "@/lib/logModel";
import { LogViewer } from "./LogViewer";

const ESC = String.fromCharCode(27);
const chunk = (seq: number, n: number): LogChunk => ({
  phase: "applying",
  seq,
  section: null,
  lines: Array.from({ length: n }, (_, i) => ({
    t: "20:00:00",
    msg: `${ESC}[1maws_subnet.private[${seq * n + i}]${ESC}[0m: Creating...`,
  })),
});

describe("LogViewer — search", () => {
  it("strips ANSI once per line, not again on each appended chunk", () => {
    const strip = vi.spyOn(Anser, "ansiToText");
    let chunks = mergeChunks([], [chunk(0, 50)]);
    const view = render(<LogViewer chunks={chunks} live className="h-[400px]" />);
    fireEvent.change(screen.getByPlaceholderText(/search logs/i), { target: { value: "creating" } });
    expect(screen.getByText("1/50")).toBeInTheDocument();
    const afterFirstSearch = strip.mock.calls.length;
    expect(afterFirstSearch).toBeGreaterThanOrEqual(50);

    chunks = mergeChunks(chunks, [chunk(1, 50)]);
    view.rerender(<LogViewer chunks={chunks} live className="h-[400px]" />);
    expect(screen.getByText("1/100")).toBeInTheDocument();
    // Only the 50 new lines were stripped.
    expect(strip.mock.calls.length - afterFirstSearch).toBe(50);
    strip.mockRestore();
  });
});
