import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { ApiError } from "@/api/client";
import { stacks, type NewStack } from "@/api/resources";
import type { Tool } from "@/api/types";
import { useSpaces } from "@/app/SpaceContext";
import { Button, Card, Field, PageTitle, Select, TextInput } from "@/components/ui";

function CreateStackForm({ onDone }: { onDone: () => void }) {
  const qc = useQueryClient();
  const { current } = useSpaces();
  const [form, setForm] = useState<NewStack>({
    name: "",
    repo_url: "",
    tool: "opentofu",
    tool_version: "1.12.0",
  });
  const create = useMutation({
    // Create in the active space (§6, Phase F); the server defaults to the bootstrap space if unset.
    mutationFn: () => stacks.create({ ...form, space_id: current?.id }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["stacks"] });
      onDone();
    },
  });

  return (
    <Card>
      <form
        className="flex flex-col gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
      >
        <Field label="Name">
          <TextInput value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} required />
        </Field>
        <Field label="Repository URL">
          <TextInput
            value={form.repo_url}
            placeholder="file:///repos/demo-network"
            onChange={(e) => setForm({ ...form, repo_url: e.target.value })}
            required
          />
        </Field>
        <div className="flex gap-3">
          <Field label="Tool">
            <Select value={form.tool} onChange={(e) => setForm({ ...form, tool: e.target.value as Tool })}>
              <option value="opentofu">opentofu</option>
              <option value="terraform">terraform</option>
            </Select>
          </Field>
          <Field label="Version">
            <TextInput value={form.tool_version} onChange={(e) => setForm({ ...form, tool_version: e.target.value })} />
          </Field>
        </div>
        <div className="flex items-center gap-2">
          <Button type="submit" variant="accent" disabled={create.isPending}>
            Create stack
          </Button>
          <Button type="button" onClick={onDone}>
            Cancel
          </Button>
          {create.error instanceof ApiError && (
            <span className="text-[12px]" style={{ color: "var(--color-state-failed)" }}>
              {create.error.detail ?? create.error.title}
            </span>
          )}
        </div>
      </form>
    </Card>
  );
}

export function StacksPage() {
  const [creating, setCreating] = useState(false);
  const { current } = useSpaces();
  const { data: all, isLoading } = useQuery({ queryKey: ["stacks"], queryFn: stacks.list });
  // The server already returns only reachable stacks; narrow to the active space when one is chosen.
  const list = current ? all?.filter((s) => s.space_id === current.id) : all;

  return (
    <div>
      <div className="mb-4 flex items-center justify-between">
        <PageTitle>Stacks</PageTitle>
        {!creating && (
          <Button variant="accent" onClick={() => setCreating(true)}>
            New stack
          </Button>
        )}
      </div>

      {creating && (
        <div className="mb-4">
          <CreateStackForm onDone={() => setCreating(false)} />
        </div>
      )}

      {isLoading ? (
        <p className="font-data text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
          Loading…
        </p>
      ) : !list || list.length === 0 ? (
        <p className="text-[13px]" style={{ color: "var(--color-text-secondary)" }}>
          No stacks yet. Connect a repository to start.
        </p>
      ) : (
        <table className="w-full text-left">
          <thead>
            <tr className="font-data text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
              <th className="border-b px-2.5 py-1.5" style={{ borderColor: "var(--color-border)" }}>NAME</th>
              <th className="border-b px-2.5 py-1.5" style={{ borderColor: "var(--color-border)" }}>REPO</th>
              <th className="border-b px-2.5 py-1.5" style={{ borderColor: "var(--color-border)" }}>TOOL</th>
            </tr>
          </thead>
          <tbody>
            {list.map((s) => (
              <tr key={s.id} style={{ borderColor: "var(--color-border)" }}>
                <td className="border-b px-2.5 py-1.5 text-[13px]" style={{ borderColor: "var(--color-border)" }}>
                  <Link to={`/stacks/${s.id}`} style={{ color: "var(--color-accent)" }}>
                    {s.name}
                  </Link>
                </td>
                <td className="font-data border-b px-2.5 py-1.5 text-[12px]" style={{ borderColor: "var(--color-border)" }}>
                  {s.repo_url}
                </td>
                <td className="font-data border-b px-2.5 py-1.5 text-[12px]" style={{ borderColor: "var(--color-border)" }}>
                  {s.tool} {s.tool_version}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
