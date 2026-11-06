import { useId, useState } from "react";
import { Download, FileUp } from "lucide-react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, saveBlob } from "@/api/client";
import { spaces as spacesApi } from "@/api/resources";
import type { ConfigDocument, Space } from "@/api/types";
import { useSpaces } from "@/app/SpaceContext";
import { useIsAdmin } from "@/auth/session";
import { Button } from "@/ui/Button";
import { Dialog, DialogClose, DialogContent, DialogTrigger } from "@/ui/overlays";
import { Banner, NativeSelect, RelativeTime, Skeleton } from "@/ui/primitives";
import { ProblemBanner } from "@/ui/ProblemBanner";
import { docOverview, parseConfigFile } from "./importReport";
import { ImportReportView } from "./ImportReportView";

// Configuration export / import of a space (SPECS §22, DESIGN §5.10) — space admin. The export is
// downloaded through an object URL; the import file is read in memory (FileReader), dry-run first,
// then applied. Nothing is kept anywhere once the dialog closes: the document, the reports and a
// created worker pool's token live in this component's state, their mutations are not cached.

interface PickedFile {
  name: string;
  doc: ConfigDocument;
}

function readText(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => reject(reader.error ?? new Error("The file could not be read."));
    reader.readAsText(file);
  });
}

/** Everything a query may cache except the session (an import touches most of a space). */
const notSession = (q: { queryKey: readonly unknown[] }) => q.queryKey[0] !== "session";

export function ImportConfigDialog({ space }: { space: Space }) {
  const qc = useQueryClient();
  const inputId = useId();
  const [open, setOpen] = useState(false);
  const [picked, setPicked] = useState<PickedFile | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const dryRun = useMutation({
    mutationFn: (doc: ConfigDocument) => spacesApi.importConfig(space.id, doc, true),
    gcTime: 0,
  });
  const apply = useMutation({
    mutationFn: (doc: ConfigDocument) => spacesApi.importConfig(space.id, doc, false),
    // The final report may carry a created worker pool's token: never kept past the dialog.
    gcTime: 0,
    onSuccess: () => qc.invalidateQueries({ predicate: notSession }),
  });
  const reset = () => {
    setPicked(null);
    setFileError(null);
    dryRun.reset();
    apply.reset();
  };
  const onFile = async (file: File | undefined) => {
    reset();
    if (!file) return;
    try {
      const parsed = parseConfigFile(await readText(file));
      if (parsed.ok) setPicked({ name: file.name, doc: parsed.doc });
      else setFileError(parsed.error);
    } catch (err) {
      setFileError((err as Error).message);
    }
  };
  const overview = picked ? docOverview(picked.doc) : null;
  const report = apply.data ?? dryRun.data;
  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        setOpen(o);
        if (!o) reset();
      }}
    >
      <DialogTrigger asChild>
        <Button variant="secondary" icon={<FileUp size={14} strokeWidth={1.5} aria-hidden />}>
          Import configuration…
        </Button>
      </DialogTrigger>
      <DialogContent
        wide
        title={`Import a configuration into ${space.name}`}
        description="Merges a stackd.config document by name: creates what is missing, updates what differs, never deletes. It carries no secret. Run a dry run first: nothing is written until you apply."
      >
        <div className="flex flex-col gap-3">
          {!apply.data && (
            <div className="flex flex-col gap-1">
              <label htmlFor={inputId} className="eyebrow text-muted">
                Configuration file (.json)
              </label>
              <input
                id={inputId}
                type="file"
                accept=".json,application/json"
                className="text-ui file:mr-3 file:h-8 file:rounded-base file:border file:border-control file:bg-surface file:px-3 file:text-ui file:text-primary"
                onChange={(e) => void onFile(e.target.files?.[0])}
              />
            </div>
          )}
          {fileError && (
            <Banner tone="failed" role="alert" title="This file cannot be imported">
              {fileError}
            </Banner>
          )}
          {overview && picked && (
            <p className="text-[12px] text-muted">
              <span className="font-data text-primary">{picked.name}</span>
              {overview.source && (
                <>
                  {" "}
                  · from space <span className="font-data text-primary">{overview.source}</span>
                </>
              )}
              {overview.exportedAt && (
                <>
                  {" "}
                  · exported <RelativeTime value={overview.exportedAt} />
                </>
              )}{" "}
              · {overview.stacks} stack{overview.stacks === 1 ? "" : "s"}, {overview.environments} environment
              {overview.environments === 1 ? "" : "s"}, {overview.variableSets} variable set
              {overview.variableSets === 1 ? "" : "s"}, {overview.dependencies}{" "}
              {overview.dependencies === 1 ? "dependency" : "dependencies"}
            </p>
          )}

          {report && <ImportReportView report={report} />}
          {dryRun.isError && <ProblemBanner error={dryRun.error} title="The dry run was refused" />}
          {apply.isError && <ProblemBanner error={apply.error} title="Nothing was imported" />}

          <div className="flex flex-wrap justify-end gap-2">
            {apply.data ? (
              <DialogClose asChild>
                <Button variant="primary">Done</Button>
              </DialogClose>
            ) : (
              <>
                <Button
                  variant={dryRun.data ? "secondary" : "primary"}
                  disabled={!picked}
                  loading={dryRun.isPending}
                  onClick={() => {
                    apply.reset();
                    if (picked) dryRun.mutate(picked.doc);
                  }}
                >
                  {dryRun.data ? "Run the dry run again" : "Run a dry run"}
                </Button>
                <Button
                  variant="primary"
                  disabled={!picked || !dryRun.data || dryRun.isPending}
                  loading={apply.isPending}
                  onClick={() => picked && apply.mutate(picked.doc)}
                  title={dryRun.data ? undefined : "Run a dry run first"}
                >
                  Apply import
                </Button>
              </>
            )}
          </div>
          {!dryRun.data && picked && (
            <p className="text-right text-[12px] text-muted">Apply is offered after a dry run.</p>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

/**
 * Export / import for one space. Space admins only: an instance admin reaches every space; anyone
 * else is a space admin when the members list (space admin only) answers — the effective space role
 * is not readable otherwise (lib/permissions.ts).
 */
export function SpaceConfiguration() {
  const isAdmin = useIsAdmin();
  const { list: spaces, current } = useSpaces();
  const [spaceId, setSpaceId] = useState<string | null>(null);
  const space = spaces.find((s) => s.id === (spaceId ?? current?.id)) ?? spaces[0];
  const probe = useQuery({
    queryKey: ["space-members", space?.id],
    queryFn: () => spacesApi.members(space!.id),
    enabled: !!space && !isAdmin,
    retry: false,
  });
  const exportConfig = useMutation({
    mutationFn: async (s: Space) => {
      const { blob, filename } = await spacesApi.exportConfig(s.id);
      saveBlob(blob, filename ?? `stackd-${s.name}.json`);
    },
  });
  if (!space) return null;
  const forbidden = probe.error instanceof ApiError && probe.error.status === 403;
  const canAdmin = isAdmin || probe.isSuccess;
  return (
    <section aria-labelledby="space-config" className="flex flex-col gap-2">
      <h2 id="space-config" className="text-section font-semibold">
        Space configuration
      </h2>
      <p className="max-w-[720px] text-[12px] text-muted">
        Export a space&apos;s configuration as a JSON document — stacks, environments, variables, variable
        sets, hooks, notifications, dependencies, by name — to review it in Git or re-create it here or on
        another instance. It never contains a secret, a state or a run, and it is no backup. Notification URLs
        travel in it: treat the file as confidential. Needs the admin role in the space.
      </p>
      <label className="flex flex-col gap-1 self-start">
        <span className="eyebrow text-muted">Space</span>
        <NativeSelect
          value={space.id}
          onChange={(e) => {
            setSpaceId(e.target.value);
            exportConfig.reset();
          }}
        >
          {spaces.map((s) => (
            <option key={s.id} value={s.id}>
              {s.name}
            </option>
          ))}
        </NativeSelect>
      </label>
      {!isAdmin && probe.isLoading ? (
        <Skeleton className="h-8 w-64" />
      ) : canAdmin ? (
        <div className="flex flex-wrap gap-2">
          <Button
            variant="secondary"
            icon={<Download size={14} strokeWidth={1.5} aria-hidden />}
            loading={exportConfig.isPending}
            onClick={() => exportConfig.mutate(space)}
          >
            Export configuration
          </Button>
          <ImportConfigDialog key={space.id} space={space} />
        </div>
      ) : forbidden ? (
        <p className="text-muted">
          Exporting or importing {space.name}&apos;s configuration needs the admin role in this space.
        </p>
      ) : probe.isError ? (
        <ProblemBanner error={probe.error} />
      ) : null}
      {exportConfig.isError && <ProblemBanner error={exportConfig.error} title="The export was refused" />}
    </section>
  );
}
