import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import {
  COMMAND_OPTIONS,
  COMMANDS_ADMIN_ONLY,
  COMMANDS_MUTATING,
  COMMANDS_READONLY,
  runs,
} from "@/api/resources";
import { Button, Card, Checkbox, Field, Select, TextInput } from "@/components/ui";
import { parseAddresses } from "./PlanOptions";

// Split an argument string respecting single/double quotes, so addresses with spaces
// (e.g. `'aws_s3_bucket.b["my key"]'`) survive as one argument.
export function parseArgs(input: string): string[] {
  const args: string[] = [];
  let cur = "";
  let quote: '"' | "'" | null = null;
  let has = false;
  for (const ch of input) {
    if (quote) {
      if (ch === quote) quote = null;
      else cur += ch;
    } else if (ch === '"' || ch === "'") {
      quote = ch;
      has = true;
    } else if (/\s/.test(ch)) {
      if (has) args.push(cur);
      cur = "";
      has = false;
    } else {
      cur += ch;
      has = true;
    }
  }
  if (has) args.push(cur);
  return args;
}

/** The args sent for a command: its allowlisted options (SPECS §4.3) first, then the positionals. */
export function buildCommandArgs(
  command: string,
  input: string,
  opts: { targets?: string; dryRun?: boolean } = {},
): string[] {
  const allowed = COMMAND_OPTIONS[command] ?? {};
  const options = [
    ...(allowed.dryRun && opts.dryRun ? ["-dry-run"] : []),
    ...(allowed.targets ? parseAddresses(opts.targets ?? "").map((a) => `-target=${a}`) : []),
  ];
  return [...options, ...parseArgs(input)];
}

const PLACEHOLDERS: Record<string, string> = {
  "force-unlock": "the lock ID, e.g. 3f2b8c1e-5a4d-4e7f-9b0a-1c2d3e4f5a6b",
  refresh: "(no arguments)",
};

// A one-off allowlisted tofu/terraform subcommand, run as a `command` run on the worker.
export function CommandPanel({
  envId,
  managedState,
  isAdmin,
}: {
  envId: string;
  /** Stackd manages the env's state: force-unlock goes through the State tab instead. */
  managedState?: boolean;
  isAdmin?: boolean;
}) {
  const navigate = useNavigate();
  const [command, setCommand] = useState("output");
  const [args, setArgs] = useState("");
  const [targets, setTargets] = useState("");
  const [dryRun, setDryRun] = useState(false);

  const mutating = COMMANDS_MUTATING.includes(command);
  const adminOnly = COMMANDS_ADMIN_ONLY.includes(command);
  const allowed = COMMAND_OPTIONS[command] ?? {};
  const unlockOnManaged = command === "force-unlock" && !!managedState;
  const run = useMutation({
    mutationFn: () => runs.command(envId, command, buildCommandArgs(command, args, { targets, dryRun })),
    onSuccess: (r) => navigate(`/runs/${r.id}`),
  });

  return (
    <Card>
      <div className="mb-1 text-[13px] font-medium">Run a command</div>
      <div className="mb-2 text-[12px] text-muted">
        One allowlisted subcommand on the worker (import, state surgery, …). Read-only commands need writer;
        mutating ones require apply rights. Options are limited to the ones offered below.
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
                  {COMMANDS_ADMIN_ONLY.includes(c) ? `${c} (admin only)` : c}
                </option>
              ))}
            </optgroup>
          </Select>
        </Field>
        <Field label={command === "force-unlock" ? "Lock ID" : "Arguments"}>
          <TextInput
            value={args}
            placeholder={PLACEHOLDERS[command] ?? "e.g. aws_s3_bucket.logs my-bucket"}
            onChange={(e) => setArgs(e.target.value)}
          />
        </Field>
        {allowed.dryRun && (
          <Checkbox checked={dryRun} onChange={setDryRun} label="Dry run (-dry-run)" className="h-8" />
        )}
        <Button type="submit" disabled={run.isPending || unlockOnManaged || (adminOnly && !isAdmin)}>
          {mutating ? "Run (mutating) →" : "Run →"}
        </Button>
      </form>
      {allowed.targets && (
        <div className="mt-2">
          <Field label="Target addresses (-target, one per line)">
            <textarea
              value={targets}
              rows={2}
              spellCheck={false}
              placeholder="aws_s3_bucket.logs"
              onChange={(e) => setTargets(e.target.value)}
              className="w-full rounded-base border border-control bg-base px-2 py-1.5 font-data text-[12px] text-primary placeholder:text-muted"
            />
          </Field>
        </div>
      )}
      {command === "force-unlock" && (
        <div className="mt-2 text-[12px] text-muted">
          {unlockOnManaged ? (
            <>
              Stackd manages this environment&apos;s state and owns its lock: release it from the State tab
              (admin) instead.
            </>
          ) : (
            <>
              Admin only. Releases a stuck lock on the environment&apos;s own backend (e.g. S3 + DynamoDB);
              the worker runs it non-interactively (<span className="font-data">-force</span>).
              {!isAdmin && " You are not an admin."}
            </>
          )}
        </div>
      )}
      {run.isError && (
        <div className="mt-2 font-data text-[12px] text-st-failed">{(run.error as Error).message}</div>
      )}
    </Card>
  );
}
