import type { Environment, Run, User } from "@/api/types";
import { ADMIN, ALICE, BOB, TIERS, makeEnv } from "@/lib/__fixtures__/factories";
import { runRights, whoMayConfirm } from "@/lib/permissions";
import { RunActionBar } from "./RunActionBar";
import { ENV_PROD, ENV_STAGING, run } from "./stories.fixtures";

export default { title: "Identity / RunActionBar" };

const noop = () => {};
const CASCADE = [
  { label: "demo-app/prod", likelihood: "plans_if_outputs_change" as const, outputs: ["network_name"] },
];

function Bar({
  r,
  env = ENV_PROD,
  me = ALICE,
  label,
  extra = {},
}: {
  r: Run;
  env?: Environment;
  me?: User;
  label: string;
  extra?: Partial<Parameters<typeof RunActionBar>[0]>;
}) {
  const tier = TIERS.find((t) => t.name === env.tier);
  const rights = runRights(r, env, tier, me);
  return (
    <div className="mb-6">
      <div className="eyebrow mb-1 text-muted">{label}</div>
      <div className="border border-line">
        <RunActionBar
          run={r}
          env={env}
          rights={rights}
          me={me}
          triggerer="bob@dev.local"
          confirmer="alice@dev.local"
          whoMayConfirm={whoMayConfirm(r, env, rights, "bob@dev.local")}
          cascade={CASCADE}
          destroyed={["terraform_data.nat_gateway"]}
          replaced={[]}
          instanceCaveat
          onConfirm={noop}
          onDiscard={noop}
          onCancel={noop}
          onReplan={noop}
          {...extra}
        />
      </div>
    </div>
  );
}

export const AwaitingDecision = () => (
  <>
    <Bar
      r={run("unconfirmed")}
      label="needs you — alice, prod four-eyes (Confirm opens the typed-name popover)"
      extra={{ nextDecision: "/runs/next" }}
    />
    <Bar r={run("unconfirmed")} me={BOB} label="waiting — bob triggered it and is a writer" />
    <Bar
      r={{ ...run("unconfirmed"), trigger_user_id: ALICE.id }}
      label="waiting — alice triggered it herself"
      extra={{
        triggerer: "alice@dev.local",
        whoMayConfirm: "an approver with tier prod other than alice@dev.local",
      }}
    />
    <Bar r={run("unconfirmed")} env={ENV_STAGING} label="ordinary env — one click, no friction" />
    <Bar r={run("mocked unconfirmed")} label="mocked — blocked for everyone" />
    <Bar
      r={{ ...run("unconfirmed"), used_secret_fallback: true }}
      me={ADMIN}
      label="fallback secret — blocked"
    />
    <Bar r={run("destroy unconfirmed")} env={ENV_STAGING} label="destroy without the destroy right" />
    <Bar r={run("destroy unconfirmed")} env={ENV_STAGING} me={ADMIN} label="destroy, admin — typed name" />
    <Bar
      r={{ ...run("unconfirmed"), triggered_by: "dependency", trigger_user_id: null }}
      label="cascade run — no human triggerer"
    />
  </>
);

export const InFlight = () => (
  <>
    <Bar r={run("queued")} label="queued — Cancel available" />
    <Bar r={run("planning")} label="planning — cancel not supported while executing" />
    <Bar r={run("unconfirmed")} label="confirm in flight" extra={{ pending: { confirm: true } }} />
    <Bar
      r={run("unconfirmed")}
      label="API refusal shown verbatim"
      extra={{ apiError: "You triggered this run (4-eyes required)." }}
    />
  </>
);

export const Ended = () => (
  <>
    <Bar r={run("applied")} label="applied" />
    <Bar r={run("no changes")} label="no changes" />
    <Bar r={run("failed during apply")} label="failed during apply" />
    <Bar r={run("failed at check")} label="failed at check" />
    <Bar r={run("discarded")} label="discarded" />
    <Bar r={run("proposed (PR) planned")} env={makeEnv({ protected: false })} label="PR plan" />
  </>
);
