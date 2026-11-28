import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { stateApi, type StateLock } from "@/api/resources";
import { makeEnv } from "@/lib/__fixtures__/factories";
import { EnvState } from "./EnvState";

vi.mock("@/api/stream", () => ({ useLiveSignals: vi.fn() }));

const LOCK: StateLock = {
  lock_id: "3f1c-lock",
  run_id: "01a1276c-0000-7000-8000-000000000001",
  locked_at: new Date(Date.now() - 5 * 60_000).toISOString(),
  operation: "OperationTypeApply",
  who: "runner@worker-1",
  version: "1.12.0",
  created: null,
  info: null,
};

function renderState(isAdmin: boolean, env = makeEnv({ managed_state: true, locked: false })) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <EnvState env={env} isAdmin={isAdmin} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.restoreAllMocks());

describe("EnvState lock", () => {
  it("shows the held state lock with its holder run, even when the env itself is not locked", async () => {
    vi.spyOn(stateApi, "versions").mockResolvedValue([]);
    vi.spyOn(stateApi, "lock").mockResolvedValue(LOCK);
    renderState(true);
    expect(await screen.findByText("⚿ locked")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "run 00000001" })).toHaveAttribute(
      "href",
      "/runs/01a1276c-0000-7000-8000-000000000001",
    );
    expect(screen.getByText(/· apply/)).toBeInTheDocument();
    expect(screen.getByText(/runner@worker-1/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Force unlock…" })).toBeInTheDocument();
  });

  it("names a run-less holder", async () => {
    vi.spyOn(stateApi, "versions").mockResolvedValue([]);
    vi.spyOn(stateApi, "lock").mockResolvedValue({ ...LOCK, run_id: null });
    renderState(true);
    expect(await screen.findByText(/no run \(an import session/)).toBeInTheDocument();
  });

  it("hides the force-unlock from non-admins", async () => {
    vi.spyOn(stateApi, "versions").mockResolvedValue([]);
    vi.spyOn(stateApi, "lock").mockResolvedValue(LOCK);
    renderState(false);
    expect(await screen.findByText("⚿ locked")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Force unlock…" })).not.toBeInTheDocument();
  });

  it("offers no force-unlock when no state lock is held, whatever env.locked says", async () => {
    vi.spyOn(stateApi, "versions").mockResolvedValue([]);
    vi.spyOn(stateApi, "lock").mockResolvedValue(null);
    renderState(true, makeEnv({ managed_state: true, locked: true }));
    expect(await screen.findByText("not held")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Force unlock…" })).not.toBeInTheDocument();
  });

  it("force-unlocks behind the typed env name, then reloads the lock", async () => {
    vi.spyOn(stateApi, "versions").mockResolvedValue([]);
    const lock = vi.spyOn(stateApi, "lock").mockResolvedValueOnce(LOCK).mockResolvedValue(null);
    const unlock = vi.spyOn(stateApi, "forceUnlock").mockResolvedValue(undefined);
    renderState(true);
    fireEvent.click(await screen.findByRole("button", { name: "Force unlock…" }));
    expect(screen.getByText("3f1c-lock")).toBeInTheDocument();
    const confirm = screen.getByRole("button", { name: "Force unlock" });
    expect(confirm).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/to confirm/), { target: { value: "prod" } });
    fireEvent.click(confirm);
    await waitFor(() => expect(unlock).toHaveBeenCalledWith("env-prod"));
    expect(await screen.findByText("not held")).toBeInTheDocument();
    expect(lock).toHaveBeenCalledTimes(2);
  });
});
