import type { Run } from "@/api/types";
import { makeEnv } from "@/lib/__fixtures__/factories";
import { summarizeEnv } from "@/lib/envSummary";
import { EnvCell } from "./EnvCell";
import { run } from "./stories.fixtures";

export default { title: "Identity / EnvCell" };

const cell = (
  name: string,
  runs: Run[],
  env = makeEnv(),
  extra: Partial<Parameters<typeof EnvCell>[0]> = {},
) => (
  <div key={name} className="flex flex-col gap-1">
    <span className="eyebrow text-muted">{name}</span>
    <div className="w-[220px] border border-line bg-surface">
      <EnvCell summary={summarizeEnv(env, runs)} {...extra} />
    </div>
  </div>
);

export const States = () => (
  <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
    {cell("needs you", [run("unconfirmed")], undefined, { needsYou: true })}
    {cell("waiting on someone", [run("unconfirmed")])}
    {cell("applied", [run("applied")])}
    {cell("no changes", [run("no changes")])}
    {cell("planning", [run("planning")])}
    {cell("failed at check", [run("failed at check")])}
    {cell("failed during apply", [run("failed during apply")])}
    {cell("never run", [])}
    {cell("PR plan active", [{ ...run("planning"), type: "proposed" }])}
  </div>
);

export const Modifiers = () => (
  <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
    {cell("mocked", [run("mocked unconfirmed")])}
    {cell("drifted", [run("applied")], makeEnv({ drift_status: "drifted" }))}
    {cell("stale ↑", [run("applied")], makeEnv({ stale: true }))}
    {cell(
      "stale, other folder",
      [run("applied")],
      makeEnv({ stale: true, commits_ahead: 2, affects_project_root: false }),
    )}
    {cell("locked", [run("applied")], makeEnv({ locked: true }))}
    {cell(
      "everything",
      [run("mocked unconfirmed")],
      makeEnv({ stale: true, drift_status: "drifted", locked: true }),
    )}
  </div>
);

export const SelectionAndCascade = () => (
  <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
    {cell("selected", [run("applied")], undefined, { selected: true })}
    {cell("downstream: will plan", [run("applied")], undefined, { downstream: "will_plan" })}
    {cell("downstream: if outputs change", [run("applied")], undefined, {
      downstream: "plans_if_outputs_change",
    })}
  </div>
);

export const Mobile390 = () => (
  <div className="w-[358px] border border-line bg-surface">
    <EnvCell
      summary={summarizeEnv(makeEnv({ stale: true }), [run("unconfirmed")])}
      needsYou
      className="w-full"
    />
  </div>
);
