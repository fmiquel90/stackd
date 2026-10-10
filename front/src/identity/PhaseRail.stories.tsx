import { buildSegments } from "@/lib/phases";
import { PhaseRail } from "./PhaseRail";
import { SCENARIOS, run } from "./stories.fixtures";

export default { title: "Identity / PhaseRail" };

export const VerticalEveryScenario = () => (
  <div className="grid grid-cols-2 gap-6 md:grid-cols-4">
    {SCENARIOS.map((s) => (
      <div key={s.name}>
        <div className="eyebrow mb-2 text-muted">{s.name}</div>
        <PhaseRail
          segments={buildSegments(s.run, { planning: 14, applying: 42 }, "alice@dev.local")}
          onSelect={() => {}}
        />
      </div>
    ))}
  </div>
);

export const Horizontal = () => (
  <div className="flex max-w-[390px] flex-col gap-4">
    {["unconfirmed", "failed at check", "failed during apply", "no changes"].map((n) => (
      <div key={n}>
        <div className="eyebrow mb-1 text-muted">{n}</div>
        <PhaseRail variant="horizontal" segments={buildSegments(run(n), { planning: 14 })} />
      </div>
    ))}
  </div>
);

export const Mini = () => (
  <div className="flex flex-col gap-2">
    {SCENARIOS.map((s) => (
      <div key={s.name} className="flex items-center gap-3">
        <span className="w-48 font-data text-[12px] text-muted">{s.name}</span>
        <PhaseRail variant="mini" segments={buildSegments(s.run)} />
      </div>
    ))}
  </div>
);
