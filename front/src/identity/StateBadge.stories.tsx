import { ModifierChips, RunStateBadge, StateBadge } from "./StateBadge";
import { SCENARIOS } from "./stories.fixtures";

export default { title: "Identity / StateBadge" };

export const EveryRunScenario = () => (
  <div className="flex flex-col items-start gap-2">
    {SCENARIOS.map((s) => (
      <div key={s.name} className="flex items-center gap-3">
        <span className="w-48 font-data text-[12px] text-muted">{s.name}</span>
        <RunStateBadge run={s.run} />
      </div>
    ))}
  </div>
);

export const NeedsYouVersusWaiting = () => (
  <div className="flex flex-col items-start gap-2">
    <StateBadge state="unconfirmed" decision="needsYou" />
    <StateBadge state="unconfirmed" decision="waiting" />
    <StateBadge state="unconfirmed" />
  </div>
);

export const CompactForCells = () => (
  <div className="flex flex-col items-start gap-2">
    {SCENARIOS.map((s) => (
      <RunStateBadge key={s.name} run={s.run} compact />
    ))}
  </div>
);

export const Modifiers = () => (
  <div className="flex flex-col items-start gap-3">
    <ModifierChips modifiers={{ mocked: true }} />
    <ModifierChips modifiers={{ fallback: true }} />
    <ModifierChips modifiers={{ stale: { commitsAhead: 3, affectsProjectRoot: true } }} />
    <ModifierChips modifiers={{ stale: { commitsAhead: null, affectsProjectRoot: null } }} />
    <ModifierChips modifiers={{ stale: { commitsAhead: 2, affectsProjectRoot: false } }} />
    <ModifierChips modifiers={{ drift: "drifted" }} />
    <ModifierChips modifiers={{ drift: "error" }} />
    <ModifierChips modifiers={{ locked: true }} />
    <ModifierChips modifiers={{ pr: true }} />
    <div className="flex items-center gap-2">
      <StateBadge state="unconfirmed" decision="needsYou" />
      <ModifierChips
        modifiers={{
          mocked: true,
          fallback: true,
          stale: { commitsAhead: 1, affectsProjectRoot: true },
          drift: "drifted",
          locked: true,
        }}
      />
    </div>
  </div>
);
