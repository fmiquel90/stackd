import { useQuery } from "@tanstack/react-query";
import { environments, runs, stacks } from "@/api/resources";
import { useSpaces } from "./SpaceContext";

/**
 * A run with the names that identify it (space / stack / env), shared by the breadcrumb and the tab
 * title. The query keys are the run page's, so this adds no request on `/runs/:id`. Read-only: the
 * breadcrumb alone pins the entity's space (`useEntitySpace`), so two readers never race on it.
 */
export function useRunIdentity(runId: string) {
  const run = useQuery({ queryKey: ["run", runId], queryFn: () => runs.get(runId) });
  const env = useQuery({
    queryKey: ["environment", run.data?.environment_id],
    queryFn: () => environments.get(run.data!.environment_id),
    enabled: Boolean(run.data?.environment_id),
  });
  const stack = useQuery({
    queryKey: ["stack", env.data?.stack_id],
    queryFn: () => stacks.get(env.data!.stack_id),
    enabled: Boolean(env.data?.stack_id),
  });
  const { list } = useSpaces();
  const space = list.find((s) => s.id === stack.data?.space_id);
  return { run: run.data, env: env.data, stack: stack.data, space };
}
