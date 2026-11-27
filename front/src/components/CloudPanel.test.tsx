import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { CloudIntegration } from "@/api/resources";
import type { Role, User } from "@/api/types";
import { CloudPanel, profileRowsError, profilesFromRows, rowsFromProfiles } from "./CloudPanel";

const STATE = "arn:aws:iam::210987654321:role/tfstates-plan";
const STATE_APPLY = "arn:aws:iam::210987654321:role/tfstates-apply";

const INTEGRATION: CloudIntegration = {
  id: "ci1",
  provider: "aws",
  plan_role_arn: "arn:aws:iam::123456789012:role/plan",
  apply_role_arn: "arn:aws:iam::123456789012:role/apply",
  region: "eu-west-1",
  session_duration: 3600,
  profiles: { tfstates: { plan_role_arn: STATE, apply_role_arn: STATE_APPLY, region: null } },
  warnings: [],
};

function renderAs(role: Role) {
  const user: User = {
    id: "u1",
    kind: "human",
    email: `${role}@dev.local`,
    display_name: null,
    avatar_url: null,
    role,
    allowed_tiers: [],
    can_destroy: false,
    disabled: false,
    onboarded: true,
    last_login_at: null,
  };
  const qc = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } });
  qc.setQueryData(["session"], user);
  qc.setQueryData(["cloud-integration", "e1"], INTEGRATION);
  render(
    <QueryClientProvider client={qc}>
      <CloudPanel envId="e1" />
    </QueryClientProvider>,
  );
}

describe("named AWS profiles (SPECS §10.6)", () => {
  it("round-trips the API map through editable rows, sorted, an empty region as null", () => {
    const rows = rowsFromProfiles({
      "x-tfstates": { plan_role_arn: STATE, apply_role_arn: STATE_APPLY, region: null },
      "a-admin": { plan_role_arn: STATE, apply_role_arn: STATE_APPLY, region: "us-east-1" },
    });
    expect(rows.map((r) => r.name)).toEqual(["a-admin", "x-tfstates"]);
    expect(rows[1].region).toBe("");
    expect(profilesFromRows([{ ...rows[1], name: " x-tfstates " }])).toEqual({
      "x-tfstates": { plan_role_arn: STATE, apply_role_arn: STATE_APPLY, region: null },
    });
  });

  it("blocks a nameless, duplicated or reserved profile before the API sees it", () => {
    const row = { name: "a", plan_role_arn: STATE, apply_role_arn: STATE, region: "" };
    expect(profileRowsError([row])).toBeNull();
    expect(profileRowsError([{ ...row, name: " " }])).toMatch(/needs a name/);
    expect(profileRowsError([row, row])).toMatch(/listed twice/);
    expect(profileRowsError([{ ...row, name: "default" }])).toMatch(/reserved/);
  });

  it("is editable by an admin", () => {
    renderAs("admin");
    expect(screen.getByDisplayValue("tfstates")).toBeInTheDocument();
    expect(screen.getByDisplayValue(STATE_APPLY)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add profile" })).toBeInTheDocument();
  });

  it("is listed read-only for everyone else", () => {
    renderAs("writer");
    expect(screen.getByText(/profile tfstates: plan/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add profile" })).toBeNull();
  });
});
