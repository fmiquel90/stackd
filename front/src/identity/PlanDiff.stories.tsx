import { useState } from "react";
import mixed from "@/lib/__fixtures__/plan-mixed.txt?raw";
import { PlanDiff } from "./PlanDiff";
import { bigPlan } from "./stories.fixtures";

export default { title: "Identity / PlanDiff" };

function Demo({
  lines,
  counts,
  initial = "",
}: {
  lines: string[] | null;
  counts: Parameters<typeof PlanDiff>[0]["counts"];
  initial?: string;
}) {
  const [q, setQ] = useState(initial);
  return (
    <PlanDiff
      lines={lines}
      counts={counts}
      query={q}
      onQueryChange={setQ}
      emptyHint="Open the logs to see what happened."
    />
  );
}

const MIXED = mixed.replace(/\n$/, "").split("\n");

export const MixedPlan = () => <Demo lines={MIXED} counts={{ add: 1, change: 1, destroy: 1 }} />;

export const SearchKeepsDestroysVisible = () => (
  <Demo lines={MIXED} counts={{ add: 1, change: 1, destroy: 1 }} initial="subnet_db" />
);

export const TagOnlyAndReplace = () => (
  <Demo
    counts={{ add: 0, change: 2, destroy: 1 }}
    lines={[
      "  # aws_s3_bucket.logs will be updated in-place",
      '  ~ resource "aws_s3_bucket" "logs" {',
      "      ~ tags     = {",
      '          ~ "owner" = "team-a" -> "team-b"',
      "        }",
      "    }",
      "",
      "  # aws_instance.web must be replaced",
      '-/+ resource "aws_instance" "web" {',
      '      ~ ami           = "ami-0a1" -> "ami-0b2" # forces replacement',
      '      ~ instance_type = "t3.small" -> "t3.medium"',
      "      password      = (sensitive value)",
      "    }",
      "",
      "Plan: 1 to add, 1 to change, 1 to destroy.",
    ]}
  />
);

export const UnparsedFallback = () => (
  <Demo
    counts={null}
    lines={[
      "Warning: a format this parser does not know",
      "  # module.x.y has changed",
      '  ~ resource "y" "z" {',
      "    }",
    ]}
  />
);

export const LongPlan500 = () => <Demo lines={bigPlan(500)} counts={{ add: 400, change: 50, destroy: 10 }} />;

export const NoPlanOutput = () => <Demo lines={null} counts={null} />;
