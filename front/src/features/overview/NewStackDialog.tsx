import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { Plus } from "lucide-react";
import { ApiError } from "@/api/client";
import { stacks, type CheckRepoResult, type NewStack } from "@/api/resources";
import type { Stack, Tool } from "@/api/types";
import { Button } from "@/ui/Button";
import { Dialog, DialogContent, DialogTrigger } from "@/ui/overlays";
import { Banner, Field, Input, NativeSelect } from "@/ui/primitives";

// New stack (DESIGN §5.7): numbered steps; the repository check runs right after creation (step 2),
// not at the end of a long form.

export function NewStackDialog({ spaceId, spaceName }: { spaceId: string; spaceName: string }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState<NewStack>({
    name: "",
    repo_url: "",
    tool: "opentofu",
    tool_version: "1.12.0",
    project_root: "",
  });
  const [created, setCreated] = useState<Stack | null>(null);
  const [check, setCheck] = useState<CheckRepoResult | null>(null);

  const create = useMutation({
    mutationFn: () =>
      stacks.create({ ...form, space_id: spaceId, project_root: form.project_root || undefined }),
    onSuccess: async (s) => {
      setCreated(s);
      qc.invalidateQueries({ queryKey: ["stacks"] });
      try {
        setCheck(await stacks.checkRepo(s.id));
      } catch (e) {
        setCheck({
          ok: false,
          branches: [],
          detail: e instanceof ApiError ? (e.detail ?? e.title) : String(e),
        });
      }
    },
  });

  const reset = (v: boolean) => {
    setOpen(v);
    if (!v) {
      setCreated(null);
      setCheck(null);
      create.reset();
      setForm({ name: "", repo_url: "", tool: "opentofu", tool_version: "1.12.0", project_root: "" });
    }
  };

  return (
    <Dialog open={open} onOpenChange={reset}>
      <DialogTrigger asChild>
        <Button variant="primary" icon={<Plus size={14} aria-hidden />}>
          New stack
        </Button>
      </DialogTrigger>
      <DialogContent
        title={created ? "2 · Repository check" : "1 · New stack"}
        description={`In space ${spaceName}.`}
      >
        {!created ? (
          <form
            className="flex flex-col gap-3"
            onSubmit={(e) => {
              e.preventDefault();
              create.mutate();
            }}
          >
            <Field label="Name" help="Unique in the space; used in URLs.">
              <Input
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                required
                autoFocus
              />
            </Field>
            <Field label="Repository URL">
              <Input
                value={form.repo_url}
                placeholder="https://github.com/org/infra.git"
                onChange={(e) => setForm({ ...form, repo_url: e.target.value })}
                required
              />
            </Field>
            <Field
              label="Project root"
              help="Folder of the root module in the repository; empty for the root."
            >
              <Input
                value={form.project_root ?? ""}
                placeholder="network/"
                onChange={(e) => setForm({ ...form, project_root: e.target.value })}
              />
            </Field>
            <div className="flex gap-3">
              <Field label="Tool">
                <NativeSelect
                  value={form.tool}
                  onChange={(e) => setForm({ ...form, tool: e.target.value as Tool })}
                >
                  <option value="opentofu">opentofu</option>
                  <option value="terraform">terraform</option>
                </NativeSelect>
              </Field>
              <Field label="Version">
                <Input
                  value={form.tool_version}
                  onChange={(e) => setForm({ ...form, tool_version: e.target.value })}
                  required
                  className="w-28"
                />
              </Field>
            </div>
            {create.error && (
              <Banner tone="failed" role="alert" title="The stack was not created">
                {create.error instanceof ApiError
                  ? (create.error.detail ?? create.error.title)
                  : String(create.error)}
              </Banner>
            )}
            <div className="flex justify-end gap-2">
              <Button variant="ghost" onClick={() => reset(false)}>
                Cancel
              </Button>
              <Button type="submit" variant="primary" loading={create.isPending}>
                Create and check the repository
              </Button>
            </div>
          </form>
        ) : (
          <div className="flex flex-col gap-3">
            {!check ? (
              <p className="text-muted">Checking that the repository is reachable…</p>
            ) : check.ok ? (
              <Banner title="Repository reachable">
                Branches:{" "}
                <span className="font-data">{check.branches.slice(0, 12).join(", ") || "none"}</span>
                {check.branches.length > 12 ? ` (+${check.branches.length - 12})` : ""}
              </Banner>
            ) : (
              <Banner tone="failed" title="The repository could not be reached">
                {check.detail} Fix the URL or the credentials in the stack settings.
              </Banner>
            )}
            <div className="flex justify-end gap-2">
              <Link
                to={`/s/${encodeURIComponent(spaceName)}/stacks/${encodeURIComponent(created.name)}`}
                onClick={() => reset(false)}
                className="inline-flex h-8 items-center rounded-base bg-primary px-3 text-onfill"
              >
                Open {created.name} — add environments
              </Link>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
