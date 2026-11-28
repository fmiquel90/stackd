import { describe, expect, it } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { Variable } from "@/api/types";
import { VariablesEditor } from "./VariablesEditor";

const variable = (over: Partial<Variable>): Variable => ({
  id: over.name ?? "v",
  kind: "terraform",
  name: "v",
  sensitive: false,
  hcl: false,
  value: "x",
  secret_source_id: null,
  secret_ref: null,
  secret_fallback_mode: null,
  ...over,
});

function renderEditor(vars: Variable[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <VariablesEditor
        queryKey={["vars"]}
        list={async () => vars}
        add={async () => undefined}
        remove={async () => undefined}
      />
    </QueryClientProvider>,
  );
}

describe("variable kinds (SPECS §3.3)", () => {
  it("groups variables by kind, each shown with what it becomes in the run", async () => {
    renderEditor([
      variable({ name: "region", kind: "terraform", value: "eu-west-1" }),
      variable({ name: "AWS_REGION", kind: "environment", value: "eu-west-1" }),
    ]);
    const tf = await screen.findByRole("region", { name: "Terraform variables" });
    const env = screen.getByRole("region", { name: "Shell environment variables" });
    expect(within(tf).getByText("var.region")).toBeInTheDocument();
    expect(within(env).getByText("$AWS_REGION")).toBeInTheDocument();
    expect(within(tf).queryByText("AWS_REGION")).toBeNull();
  });

  it("explains the chosen kind and previews the reference while adding", async () => {
    renderEditor([]);
    await screen.findByText("No variables yet.");
    expect(screen.getByText(/your code reads it as var\.<name>/)).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText("Shell environment variable"));
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "DD_API_KEY" } });
    expect(screen.getByText("$DD_API_KEY")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add shell environment variable" })).toBeInTheDocument();
  });
});
