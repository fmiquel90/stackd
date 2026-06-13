import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { variableSets } from "@/api/resources";
import { Button, Card, Field, PageTitle, TextInput } from "@/components/ui";

function CreateForm({ onDone }: { onDone: () => void }) {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [autoAttach, setAutoAttach] = useState(false);
  const create = useMutation({
    mutationFn: () => variableSets.create({ name, auto_attach: autoAttach }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["variable-sets"] });
      onDone();
    },
  });
  return (
    <Card>
      <form
        className="flex items-end gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          create.mutate();
        }}
      >
        <Field label="Name">
          <TextInput value={name} onChange={(e) => setName(e.target.value)} placeholder="common-aws" required />
        </Field>
        <label className="flex items-center gap-2 text-[13px]">
          <input type="checkbox" checked={autoAttach} onChange={(e) => setAutoAttach(e.target.checked)} />
          auto-attach
        </label>
        <Button type="submit" variant="accent" disabled={create.isPending}>
          Create set
        </Button>
      </form>
    </Card>
  );
}

export function VariableSetsPage() {
  const [creating, setCreating] = useState(false);
  const { data: list } = useQuery({ queryKey: ["variable-sets"], queryFn: variableSets.list });

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <PageTitle>Variable Sets</PageTitle>
        {!creating && (
          <Button variant="accent" onClick={() => setCreating(true)}>
            New set
          </Button>
        )}
      </div>
      {creating && <CreateForm onDone={() => setCreating(false)} />}
      <div className="flex flex-col gap-2">
        {(list ?? []).map((s) => (
          <Card key={s.id}>
            <div className="flex items-center gap-3">
              <span className="text-[14px] font-medium">{s.name}</span>
              {s.auto_attach && (
                <span className="font-data text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
                  auto-attach
                </span>
              )}
            </div>
          </Card>
        ))}
        {list && list.length === 0 && (
          <p className="text-[13px]" style={{ color: "var(--color-text-secondary)" }}>
            No variable sets yet.
          </p>
        )}
      </div>
    </div>
  );
}
