import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { environments } from "@/api/resources";
import { makeEnv } from "@/lib/__fixtures__/factories";
import { EnvSettings, parseVarFiles } from "./EnvSettings";

function renderSettings(env = makeEnv()) {
  const qc = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } });
  qc.setQueryData(["tiers"], []);
  render(
    <QueryClientProvider client={qc}>
      <EnvSettings env={env} canWrite isAdmin={false} onRenamed={() => {}} onDeleted={() => {}} />
    </QueryClientProvider>,
  );
}

afterEach(() => vi.restoreAllMocks());

describe("parseVarFiles", () => {
  it("keeps the order, trims and drops blank lines", () => {
    expect(parseVarFiles(" envs/prod.tfvars \n\n../shared/prod.tfvars\n")).toEqual([
      "envs/prod.tfvars",
      "../shared/prod.tfvars",
    ]);
    expect(parseVarFiles("")).toEqual([]);
  });
});

describe("EnvSettings variable files", () => {
  it("shows the env's var files, one per line, whatever the state backend", () => {
    renderSettings(makeEnv({ managed_state: true, var_files: ["a.tfvars", "b.tfvars"] }));
    expect((screen.getByLabelText(/Variable files/) as HTMLTextAreaElement).value).toBe("a.tfvars\nb.tfvars");
  });

  it("saves the edited list in order", async () => {
    const update = vi.spyOn(environments, "update").mockResolvedValue(makeEnv());
    renderSettings();
    fireEvent.change(screen.getByLabelText(/Variable files/), {
      target: { value: "envs/prod.tfvars\n../shared/prod.tfvars" },
    });
    fireEvent.click(screen.getByRole("button", { name: /Save settings/ }));
    await waitFor(() => expect(update).toHaveBeenCalled());
    expect(update.mock.calls[0][1].var_files).toEqual(["envs/prod.tfvars", "../shared/prod.tfvars"]);
  });
});
