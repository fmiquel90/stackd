import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { variableSets } from "@/api/resources";
import { Button, Card, Field, PageTitle, TextInput } from "@/components/ui";
import { VariablesEditor } from "@/components/VariablesEditor";

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

function SetCard({ id, name, autoAttach }: { id: string; name: string; autoAttach: boolean }) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const remove = useMutation({
    mutationFn: () => variableSets.remove(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["variable-sets"] }),
  });

  return (
    <Card>
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
            className="ui-btn cursor-pointer text-[14px] font-medium hover:underline"
            style={{ background: "transparent" }}
          >
            {name}
          </button>
          {autoAttach && (
            <span className="font-data text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
              auto-attach
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <Button onClick={() => setOpen((v) => !v)}>{open ? "Hide variables" : "Variables"}</Button>
          <Button onClick={() => remove.mutate()} disabled={remove.isPending}>
            Delete
          </Button>
        </div>
      </div>
      {/* A set attached anywhere returns 409 — surface the "detach first" reason inline. */}
      {remove.isError && (
        <div className="mt-2 font-data text-[12px]" style={{ color: "var(--color-state-failed)" }}>
          {(remove.error as Error).message}
        </div>
      )}
      {open && (
        <div className="mt-3">
          <VariablesEditor
            queryKey={["variable-set-vars", id]}
            list={() => variableSets.variables(id)}
            add={(body) => variableSets.addVariable(id, body)}
            update={(varId, body) => variableSets.updateVariable(id, varId, body)}
            remove={(varId) => variableSets.removeVariable(id, varId)}
          />
        </div>
      )}
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
          <SetCard key={s.id} id={s.id} name={s.name} autoAttach={s.auto_attach} />
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
