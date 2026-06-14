import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { COMMANDS_MUTATING, COMMANDS_READONLY, runs } from "@/api/resources";
import { Button, Card, Field, Select, TextInput } from "@/components/ui";

// A one-off allowlisted tofu/terraform subcommand, run as a `command` run on the worker.
export function CommandPanel({ envId }: { envId: string }) {
  const navigate = useNavigate();
  const [command, setCommand] = useState("output");
  const [args, setArgs] = useState("");

  const mutating = COMMANDS_MUTATING.includes(command);
  const run = useMutation({
    mutationFn: () =>
      runs.command(
        envId,
        command,
        args.trim() ? args.trim().split(/\s+/) : [],
      ),
    onSuccess: (r) => navigate(`/runs/${r.id}`),
  });

  return (
    <Card>
      <div className="mb-1 text-[13px] font-medium">Run a command</div>
      <div className="mb-2 text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
        One allowlisted subcommand on the worker (import, state surgery, …). Read-only commands need
        writer; mutating ones require apply rights.
      </div>
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          run.mutate();
        }}
      >
        <Field label="Command">
          <Select value={command} onChange={(e) => setCommand(e.target.value)}>
            <optgroup label="read-only">
              {COMMANDS_READONLY.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </optgroup>
            <optgroup label="mutating (needs apply)">
              {COMMANDS_MUTATING.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </optgroup>
          </Select>
        </Field>
        <Field label="Arguments">
          <TextInput
            value={args}
            placeholder="e.g. aws_s3_bucket.logs my-bucket"
            onChange={(e) => setArgs(e.target.value)}
          />
        </Field>
        <Button type="submit" disabled={run.isPending}>
          {mutating ? "Run (mutating) →" : "Run →"}
        </Button>
      </form>
      {run.isError && (
        <div className="mt-2 font-data text-[12px]" style={{ color: "var(--color-state-failed)" }}>
          {(run.error as Error).message}
        </div>
      )}
    </Card>
  );
}
