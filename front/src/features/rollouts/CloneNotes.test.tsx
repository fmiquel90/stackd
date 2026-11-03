import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import type { CloneResult } from "@/api/resources";
import { CloneNotes } from "./NewEnvironmentDialog";

const env = (stack: string, extra: Partial<CloneResult["environments"][number]> = {}) => ({
  id: `id-${stack}`,
  stack_id: `s-${stack}`,
  stack_name: stack,
  name: "prod-euw3",
  tier: "prod",
  autodeploy: true,
  autodeploy_off_reason: null,
  label_sets: [],
  ...extra,
});

const RESULT: CloneResult = {
  dry_run: true,
  environments: [
    env("net", {
      autodeploy: false,
      autodeploy_off_reason: "left_to_fill",
      label_sets: [
        { name: "region-euw3", new: true },
        { name: "team-core", new: false },
      ],
    }),
    env("app", { label_sets: [{ name: "region-euw3", new: true }] }),
  ],
  edges: [],
  missing_secrets: [],
  to_configure: [],
  not_copied: [
    {
      stack: "net",
      environment: "prod-euw3",
      item: "variable",
      name: "region",
      reason: "shadows_label_set",
      keys: ["region"],
      label_sets: ["region-euw3"],
    },
  ],
};

describe("CloneNotes", () => {
  it("names the sets the labels attach, marking the new ones in words", () => {
    render(<CloneNotes result={RESULT} />);
    expect(screen.getByText(/Labels attach/).closest("p")).toHaveTextContent(
      "Labels attach: region-euw3 (new: the model does not get it), team-core in net",
    );
  });

  it("lists what is not copied because it would shadow a label set, and forced-off autodeploy", () => {
    render(<CloneNotes result={RESULT} />);
    expect(screen.getByRole("note")).toHaveTextContent(
      "variable region in net/prod-euw3 — it would shadow region of region-euw3",
    );
    expect(screen.getByText(/Autodeploy off in/)).toHaveTextContent(
      "net/prod-euw3: secrets or settings left to fill",
    );
  });
});
