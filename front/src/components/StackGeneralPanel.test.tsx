import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { Role, Stack, User } from "@/api/types";
import { StackGeneralPanel } from "./StackGeneralPanel";

const STACK: Stack = {
  id: "s1",
  space_id: "sp1",
  name: "network",
  description: null,
  repo_url: "file:///repos/network",
  repo_auth_kind: "none",
  has_repo_secret: false,
  has_webhook_secret: true,
  project_root: ".",
  tool: "opentofu",
  tool_version: "1.12",
  labels: {},
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
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
  qc.setQueryData(["stack", STACK.id], STACK);
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <StackGeneralPanel stackId={STACK.id} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("StackGeneralPanel webhook secret", () => {
  it("is editable by a writer, as the API allows (PATCH /stacks/{id} is writer-level)", () => {
    renderAs("writer");
    expect(screen.getByText(/Webhook secret/)).toBeTruthy();
  });

  it("is hidden from a reader", () => {
    renderAs("reader");
    expect(screen.queryByText(/Webhook secret/)).toBeNull();
  });
});
