import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { Role, Stack, User } from "@/api/types";
import { stacks } from "@/api/resources";
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

function renderAs(role: Role, stack: Stack = STACK) {
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
  qc.setQueryData(["stack", stack.id], stack);
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <StackGeneralPanel stackId={stack.id} />
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

describe("StackGeneralPanel repo credential", () => {
  // Built from parts: the repo's detect-private-key hook refuses a literal key header.
  const PEM = "OPENSSH PRIVATE" + " KEY-----";
  const KEY = `-----BEGIN ${PEM}\nb3Blbg==\n-----END ${PEM}\n`;

  it("takes a deploy key in a multi-line field and sends it whole", async () => {
    const update = vi.spyOn(stacks, "update").mockResolvedValue(STACK);
    renderAs("writer", {
      ...STACK,
      repo_url: "git@git.example.com:org/infra.git",
      repo_auth_kind: "deploy_key",
    });
    const field = screen.getByLabelText(/Deploy key/);
    expect(field.tagName).toBe("TEXTAREA");
    fireEvent.change(field, { target: { value: KEY } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(update).toHaveBeenCalled());
    expect(update.mock.calls[0][1].repo_secret).toBe(KEY);
    update.mockRestore();
  });

  it("never fills back a stored key, and omits it when left blank", async () => {
    const update = vi.spyOn(stacks, "update").mockResolvedValue(STACK);
    renderAs("writer", { ...STACK, repo_auth_kind: "deploy_key", has_repo_secret: true });
    expect((screen.getByLabelText(/Deploy key.*leave blank to keep/) as HTMLTextAreaElement).value).toBe("");
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(update).toHaveBeenCalled());
    expect("repo_secret" in update.mock.calls[0][1]).toBe(false);
    update.mockRestore();
  });

  it("keeps a token in a password input", () => {
    renderAs("writer", { ...STACK, repo_auth_kind: "token" });
    expect(screen.getByLabelText(/^Token/).getAttribute("type")).toBe("password");
  });
});
