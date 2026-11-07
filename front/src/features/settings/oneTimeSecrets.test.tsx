import { afterEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ApiError } from "@/api/client";
import type { ApiTokenCreated, ImportReport, ServiceAccount, Space } from "@/api/types";
import { TooltipProvider } from "@/ui/overlays";

// The raw API token and a created worker pool's token are shown once, in component state only:
// closing the dialog drops them, and the mutation that returned them is not kept in the cache.

const RAW = "stkd_THE-RAW-TOKEN-VALUE";
const POOL_TOKEN = "pool-secret-token";

const mocks = vi.hoisted(() => ({
  tokens: vi.fn(),
  createToken: vi.fn(),
  importConfig: vi.fn(),
}));

vi.mock("@/api/resources", () => ({
  serviceAccounts: {
    tokens: mocks.tokens,
    createToken: mocks.createToken,
    revokeToken: vi.fn(),
    list: vi.fn(async () => []),
  },
  users: { update: vi.fn() },
  spaces: { importConfig: mocks.importConfig, members: vi.fn(), exportConfig: vi.fn() },
}));

// Imported after the mock.
const { TokensDialog } = await import("./serviceAccounts");
const { ImportConfigDialog } = await import("./spaceConfig");

const ACCOUNT: ServiceAccount = {
  id: "sa1",
  name: "ci",
  email: "ci@service-accounts.stackd.invalid",
  role: "writer",
  disabled: false,
  created_at: "2026-11-01T00:00:00Z",
  active_tokens: 0,
};

const SPACE: Space = { id: "sp1", name: "payments", description: null, created_at: "2026-01-01T00:00:00Z" };

function setup(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <TooltipProvider>{ui}</TooltipProvider>
    </QueryClientProvider>,
  );
  return qc;
}

/** Everything the query client holds, serialized: a secret must not be anywhere in it. */
function cacheDump(qc: QueryClient): string {
  return JSON.stringify([
    qc
      .getQueryCache()
      .getAll()
      .map((q) => q.state.data),
    qc
      .getMutationCache()
      .getAll()
      .map((m) => m.state.data),
  ]);
}

afterEach(() => vi.clearAllMocks());

describe("API token, shown once", () => {
  it("is displayed after creation, then gone from the screen and the cache once the dialog closes", async () => {
    mocks.tokens.mockResolvedValue([]);
    const created: ApiTokenCreated = {
      id: "t1",
      name: "gha",
      prefix: "stkd_THE",
      expires_at: "2027-02-04T00:00:00Z",
      last_used_at: null,
      revoked_at: null,
      created_at: "2026-11-06T00:00:00Z",
      token: RAW,
    };
    mocks.createToken.mockResolvedValue(created);
    const qc = setup(<TokensDialog account={ACCOUNT} />);

    fireEvent.click(screen.getByRole("button", { name: "Tokens…" }));
    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByPlaceholderText("github-actions"), { target: { value: "gha" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Create token" }));

    const field = await within(dialog).findByLabelText("Token gha");
    expect(field).toHaveValue(RAW);
    expect(field).toHaveAttribute("readonly");
    expect(within(dialog).getByText(/you won't see it again/)).toBeInTheDocument();
    expect(mocks.createToken).toHaveBeenCalledWith("sa1", { name: "gha", expires_in_days: 90 });

    // The check below can see it: while the dialog is open, the mutation still holds it.
    expect(cacheDump(qc)).toContain(RAW);
    fireEvent.click(within(dialog).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(cacheDump(qc)).not.toContain(RAW));

    fireEvent.click(screen.getByRole("button", { name: "Tokens…" }));
    const again = await screen.findByRole("dialog");
    expect(within(again).queryByDisplayValue(RAW)).toBeNull();
    expect(within(again).queryByText(/you won't see it again/)).toBeNull();
  });
});

describe("configuration import", () => {
  const report = (dry: boolean): ImportReport => ({
    dry_run: dry,
    summary: { created: 2, updated: 0, unchanged: 1, skipped: 1 },
    changes: [
      { op: "created", kind: "stack", path: "stacks/net" },
      { op: "created", kind: "worker_pool", path: "worker_pools/aws" },
      { op: "unchanged", kind: "tier", path: "tiers/prod" },
    ],
    skipped: [{ kind: "variable", path: "stacks/net/variables/terraform/pw", reason: "value_required" }],
    missing_secrets: [{ path: "stacks/net/variables/terraform/pw", item: "variable" }],
    warnings: [],
    id_map: { stacks: { a: "b" }, environments: {} },
    worker_pool_tokens: dry ? [] : [{ name: "aws", token: POOL_TOKEN }],
  });

  async function pickFile(dialog: HTMLElement, content: string) {
    const input = within(dialog).getByLabelText("Configuration file (.json)");
    fireEvent.change(input, {
      target: { files: [new File([content], "payments.json", { type: "application/json" })] },
    });
  }

  it("dry-runs, applies, shows a created pool's token once, and forgets it on close", async () => {
    mocks.importConfig.mockImplementation(async (_id: string, _doc: unknown, dry: boolean) => report(dry));
    const qc = setup(<ImportConfigDialog space={SPACE} />);
    fireEvent.click(screen.getByRole("button", { name: "Import configuration…" }));
    const dialog = await screen.findByRole("dialog");

    const apply = within(dialog).getByRole("button", { name: "Apply import" });
    expect(apply).toBeDisabled();
    await pickFile(dialog, '{"format":"stackd.config","stacks":[{"name":"net","environments":[]}]}');
    await within(dialog).findByText("payments.json");
    expect(apply).toBeDisabled(); // a dry run comes first

    fireEvent.click(within(dialog).getByRole("button", { name: "Run a dry run" }));
    await within(dialog).findByText("1 secret to fill before runs flow");
    expect(mocks.importConfig).toHaveBeenLastCalledWith(
      "sp1",
      expect.objectContaining({ stacks: expect.any(Array) }),
      true,
    );
    expect(within(dialog).getByText(/would not be imported/)).toBeInTheDocument();
    expect(within(dialog).queryByDisplayValue(POOL_TOKEN)).toBeNull();

    fireEvent.click(within(dialog).getByRole("button", { name: "Apply import" }));
    const token = await within(dialog).findByLabelText("Agent token for pool aws");
    expect(token).toHaveValue(POOL_TOKEN);
    expect(mocks.importConfig).toHaveBeenLastCalledWith("sp1", expect.anything(), false);

    fireEvent.click(within(dialog).getByRole("button", { name: "Done" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(cacheDump(qc)).not.toContain(POOL_TOKEN));

    fireEvent.click(screen.getByRole("button", { name: "Import configuration…" }));
    const again = await screen.findByRole("dialog");
    expect(within(again).queryByDisplayValue(POOL_TOKEN)).toBeNull();
    expect(within(again).getByRole("button", { name: "Apply import" })).toBeDisabled();
  });

  it("refuses a file that is not a document, without calling the API", async () => {
    setup(<ImportConfigDialog space={SPACE} />);
    fireEvent.click(screen.getByRole("button", { name: "Import configuration…" }));
    const dialog = await screen.findByRole("dialog");
    await pickFile(dialog, "not json");
    expect(await within(dialog).findByText("This file cannot be imported")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Run a dry run" })).toBeDisabled();
    expect(mocks.importConfig).not.toHaveBeenCalled();
  });

  it("lists a 422's fields at fault, never their input", async () => {
    mocks.importConfig.mockRejectedValue(
      new ApiError(422, "Validation error", "Request payload failed validation.", [
        { loc: ["body", "stacks", 0, "nmae"], msg: "Extra inputs are not permitted" },
      ]),
    );
    setup(<ImportConfigDialog space={SPACE} />);
    fireEvent.click(screen.getByRole("button", { name: "Import configuration…" }));
    const dialog = await screen.findByRole("dialog");
    await pickFile(dialog, '{"stacks":[{"nmae":"net"}]}');
    await within(dialog).findByText("payments.json");
    fireEvent.click(within(dialog).getByRole("button", { name: "Run a dry run" }));
    expect(await within(dialog).findByText("The dry run was refused")).toBeInTheDocument();
    expect(within(dialog).getByText("stacks › 0 › nmae")).toBeInTheDocument();
    expect(within(dialog).getByText(/Extra inputs are not permitted/)).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Apply import" })).toBeDisabled();
  });
});
