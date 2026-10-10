import { lazy, Suspense, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Link,
  Navigate,
  NavLink,
  Route,
  Routes,
  useLocation,
  useMatch,
  useNavigate,
  useParams,
} from "react-router-dom";
import {
  ChevronsUpDown,
  Command as CommandIcon,
  Keyboard,
  LogOut,
  Menu as MenuIcon,
  Moon,
  Sun,
} from "lucide-react";
import { environments, observability, runs, stacks } from "@/api/resources";
import { useConnectionStatus } from "@/api/stream";
import type { User } from "@/api/types";
import { useLogout } from "@/auth/session";
import { Brand } from "@/components/BrandMark";
import { NotificationBell } from "@/components/NotificationBell";
import { Button } from "@/ui/Button";
import { cn } from "@/ui/cn";
import {
  Dialog,
  DialogContent,
  Menu,
  MenuContent,
  MenuItem,
  MenuLabel,
  MenuSeparator,
  MenuTrigger,
  SheetContent,
  TooltipProvider,
} from "@/ui/overlays";
import { Kbd } from "@/ui/primitives";
import { AnnouncerProvider } from "./announcer";
import { CommandPalette } from "./CommandPalette";
import { KeyboardProvider, useHotkeys, useKeyboardSettings } from "./keyboard";
import { NAV } from "./nav";
import { SpaceProvider, useEntitySpace, useSpaces } from "./SpaceContext";
import { useSpaceEnvs } from "@/features/data/spaceEnvs";
import { STUCK_AFTER_S, useQueueRows, waitedSeconds } from "@/features/queue/queueModel";
import { useTheme } from "./theme";

// Route-level code splitting: each page is its own chunk; the graph stack loads only on /graph.
const OverviewPage = lazy(() =>
  import("@/features/overview/OverviewPage").then((m) => ({ default: m.OverviewPage })),
);
const StackRoute = lazy(() => import("@/features/stacks/StackPage").then((m) => ({ default: m.StackRoute })));
const EnvPage = lazy(() => import("@/features/environments/EnvPage").then((m) => ({ default: m.EnvPage })));
const RunPage = lazy(() => import("@/features/runs/RunPage").then((m) => ({ default: m.RunPage })));
const QueuePage = lazy(() => import("@/features/queue/QueuePage").then((m) => ({ default: m.QueuePage })));
const WorkersPage = lazy(() =>
  import("@/features/workers/WorkersPage").then((m) => ({ default: m.WorkersPage })),
);
const AuditPage = lazy(() => import("@/pages/AuditPage").then((m) => ({ default: m.AuditPage })));
const GraphPage = lazy(() => import("@/features/graph/GraphPage").then((m) => ({ default: m.GraphPage })));
const SettingsPage = lazy(() => import("@/pages/SettingsPage").then((m) => ({ default: m.SettingsPage })));
const VariableSetsPage = lazy(() =>
  import("@/pages/VariableSetsPage").then((m) => ({ default: m.VariableSetsPage })),
);

/* ---------------- Navigation ---------------- */

function NavLinks({ user, onNavigate }: { user: User; onNavigate?: () => void }) {
  const { spacePath } = useSpaces();
  const { pathname } = useLocation();
  return (
    <ul className="flex flex-col gap-0.5">
      {NAV.filter((n) => !n.adminOnly || user.role === "admin").map((n) => {
        const active = n.match(pathname);
        return (
          <li key={n.label}>
            <NavLink
              to={n.to(spacePath)}
              onClick={onNavigate}
              aria-current={active ? "page" : undefined}
              className={cn(
                "flex h-9 items-center gap-2.5 rounded-base border-l-2 px-2.5 text-ui",
                active
                  ? "border-ring bg-raised text-primary"
                  : "border-transparent text-muted hover:bg-raised hover:text-primary",
              )}
            >
              <n.icon size={17} strokeWidth={1.5} aria-hidden />
              {n.label}
              {n.label === "Queue" && <StuckCount />}
              {n.label === "Workers" && <WorkersCount />}
              {n.label === "Overview" && <NeedsYouCount user={user} />}
            </NavLink>
          </li>
        );
      })}
    </ul>
  );
}

/** Decisions that need the viewer in the current space (DESIGN §4) — the reserved human signal. */
function NeedsYouCount({ user }: { user: User }) {
  const { current } = useSpaces();
  const { needsYou } = useSpaceEnvs(current?.id, user);
  if (!needsYou.length) return null;
  return (
    <span className="ml-auto inline-flex h-5 min-w-5 items-center justify-center rounded-badge bg-st-decision px-1 font-data text-[11px] text-onfill">
      {needsYou.length}
      <span className="sr-only"> decision{needsYou.length > 1 ? "s" : ""} need you</span>
    </span>
  );
}

/** Runs waiting for a worker for over 2 minutes (DESIGN §5.5) — neutral: a wait is not a failure. */
function StuckCount() {
  const { rows } = useQueueRows(15_000);
  const now = new Date();
  const n = rows.filter((r) => r.group === "waiting" && (waitedSeconds(r, now) ?? 0) >= STUCK_AFTER_S).length;
  if (!n) return null;
  return (
    <span className="ml-auto inline-flex h-5 min-w-5 items-center justify-center rounded-badge border border-control px-1 font-data text-[11px] text-primary">
      {n}
      <span className="sr-only"> run{n > 1 ? "s" : ""} waiting over 2 minutes</span>
    </span>
  );
}

function WorkersCount() {
  const { data, isError } = useQuery({
    queryKey: ["health"],
    queryFn: observability.health,
    refetchInterval: 15_000,
  });
  if (!data && !isError) return null;
  const none = isError || (data && data.workers.online === 0);
  return (
    <span className={cn("ml-auto font-data text-[12px]", none ? "text-st-failed" : "text-muted")}>
      {isError ? "API down" : `${data!.workers.online}/${data!.workers.total}`}
      <span className="sr-only"> workers online</span>
    </span>
  );
}

function SpaceSwitcher() {
  const { list, current, setCurrentId } = useSpaces();
  if (list.length <= 1) return null;
  return (
    <Menu>
      <MenuTrigger asChild>
        <button
          type="button"
          className="flex h-9 w-full items-center gap-2 rounded-base border border-control px-2.5 text-left text-ui hover:bg-raised"
        >
          <span className="eyebrow text-muted">space</span>
          <span className="truncate font-data">{current?.name ?? "…"}</span>
          <ChevronsUpDown size={14} aria-hidden className="ml-auto text-muted" />
        </button>
      </MenuTrigger>
      <MenuContent align="start">
        <MenuLabel>Switch space</MenuLabel>
        {list.map((s) => (
          <MenuItem
            key={s.id}
            onSelect={() => setCurrentId(s.id)}
            aria-current={s.id === current?.id ? "true" : undefined}
          >
            <span className="font-data">{s.name}</span>
            {s.id === current?.id && <span className="ml-auto text-[12px] text-muted">current</span>}
          </MenuItem>
        ))}
      </MenuContent>
    </Menu>
  );
}

function UserMenu({ user, onHelp }: { user: User; onHelp: () => void }) {
  const logout = useLogout();
  const { theme, toggle } = useTheme();
  const { enabled, setEnabled } = useKeyboardSettings();
  return (
    <Menu>
      <MenuTrigger asChild>
        <button
          type="button"
          className="flex w-full flex-col items-start rounded-base px-2.5 py-1.5 text-left hover:bg-raised"
        >
          <span className="truncate font-data text-[12px]">{user.email}</span>
          <span className="font-data text-[11px] text-muted">
            {user.role} · {user.allowed_tiers.join(" ") || "no tiers"}
          </span>
        </button>
      </MenuTrigger>
      <MenuContent align="start" side="top">
        <MenuLabel>
          {user.email} · {user.role}
        </MenuLabel>
        <MenuItem onSelect={toggle}>
          {theme === "dark" ? <Sun size={14} aria-hidden /> : <Moon size={14} aria-hidden />}
          Switch to {theme === "dark" ? "light" : "dark"} theme
        </MenuItem>
        <MenuItem onSelect={onHelp}>
          <Keyboard size={14} aria-hidden /> Keyboard shortcuts <Kbd>?</Kbd>
        </MenuItem>
        <MenuItem onSelect={() => setEnabled(!enabled)}>
          <Keyboard size={14} aria-hidden /> {enabled ? "Turn off" : "Turn on"} single-key shortcuts
        </MenuItem>
        <MenuSeparator />
        <MenuItem onSelect={() => logout.mutate()}>
          <LogOut size={14} aria-hidden /> Sign out
        </MenuItem>
      </MenuContent>
    </Menu>
  );
}

function SideNav({
  user,
  onHelp,
  onNavigate,
  className,
}: {
  user: User;
  onHelp: () => void;
  onNavigate?: () => void;
  className?: string;
}) {
  return (
    <nav
      aria-label="Primary"
      className={cn("flex w-52 shrink-0 flex-col gap-3 border-r border-line bg-surface p-2", className)}
    >
      <Link to="/" className="flex items-center px-2 pt-1" aria-label="StackD home" onClick={onNavigate}>
        <Brand />
      </Link>
      <NavLinks user={user} onNavigate={onNavigate} />
      <div className="mt-auto flex flex-col gap-2 border-t border-line pt-2">
        <SpaceSwitcher />
        <UserMenu user={user} onHelp={onHelp} />
      </div>
    </nav>
  );
}

function BottomBar({ onMenu }: { onMenu: () => void }) {
  const { spacePath } = useSpaces();
  const { pathname } = useLocation();
  return (
    <nav
      aria-label="Primary (mobile)"
      className="flex h-14 shrink-0 items-stretch border-t border-line bg-surface md:hidden"
    >
      {NAV.filter((n) => n.mobile).map((n) => (
        <NavLink
          key={n.label}
          to={n.to(spacePath)}
          aria-current={n.match(pathname) ? "page" : undefined}
          className={cn(
            "flex flex-1 flex-col items-center justify-center gap-0.5 text-[11px]",
            n.match(pathname) ? "text-primary" : "text-muted",
          )}
        >
          <n.icon size={18} strokeWidth={1.5} aria-hidden />
          {n.label}
        </NavLink>
      ))}
      <button
        type="button"
        onClick={onMenu}
        className="flex flex-1 flex-col items-center justify-center gap-0.5 text-[11px] text-muted"
      >
        <MenuIcon size={18} strokeWidth={1.5} aria-hidden />
        Menu
      </button>
    </nav>
  );
}

/* ---------------- Top bar ---------------- */

function Crumb({ to, children, last }: { to?: string; children: ReactNode; last?: boolean }) {
  return (
    <li className="flex min-w-0 items-center gap-1.5">
      <span aria-hidden className="text-muted">
        /
      </span>
      {to && !last ? (
        <Link to={to} className="truncate hover:underline">
          {children}
        </Link>
      ) : (
        <span className="truncate" aria-current={last ? "page" : undefined}>
          {children}
        </span>
      )}
    </li>
  );
}

function RunCrumbs({ runId }: { runId: string }) {
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
  // The run's space, so the breadcrumb and the nav follow the entity, not the last selection.
  useEntitySpace(stack.data?.space_id);
  const { list } = useSpaces();
  const space = list.find((s) => s.id === stack.data?.space_id);
  const base = space ? `/s/${encodeURIComponent(space.name)}` : undefined;
  return (
    <>
      {space && <Crumb to={base}>{space.name}</Crumb>}
      {stack.data && (
        <Crumb to={`${base}/stacks/${encodeURIComponent(stack.data.name)}`}>{stack.data.name}</Crumb>
      )}
      {stack.data && env.data && (
        <Crumb
          to={`${base}/stacks/${encodeURIComponent(stack.data.name)}/envs/${encodeURIComponent(env.data.name)}`}
        >
          {env.data.name}
        </Crumb>
      )}
      <Crumb last>run {runId.slice(0, 8)}</Crumb>
    </>
  );
}

const PAGE_TITLES: Record<string, string> = {
  graph: "Graph",
  "variable-sets": "Variable sets",
  queue: "Queue",
  workers: "Workers",
  audit: "Audit",
  settings: "Settings",
};

function Breadcrumb() {
  const { pathname } = useLocation();
  const { current } = useSpaces();
  const runMatch = useMatch("/runs/:runId");
  const parts = pathname.split("/").filter(Boolean).map(decodeURIComponent);
  let crumbs: ReactNode;
  if (runMatch?.params.runId) crumbs = <RunCrumbs runId={runMatch.params.runId} />;
  else if (parts[0] === "s" && parts[1]) {
    const base = `/s/${encodeURIComponent(parts[1])}`;
    const rest = parts.slice(2);
    crumbs = (
      <>
        <Crumb to={base} last={rest.length === 0}>
          {parts[1]}
        </Crumb>
        {rest[0] === "stacks" && rest[1] && (
          <Crumb to={`${base}/stacks/${encodeURIComponent(rest[1])}`} last={!rest[3]}>
            {rest[1]}
          </Crumb>
        )}
        {rest[0] === "stacks" && rest[2] === "envs" && rest[3] && <Crumb last>{rest[3]}</Crumb>}
        {rest[0] !== "stacks" && rest[0] && <Crumb last>{PAGE_TITLES[rest[0]] ?? rest[0]}</Crumb>}
      </>
    );
  } else {
    crumbs = (
      <>
        {current && <Crumb to={`/s/${encodeURIComponent(current.name)}`}>{current.name}</Crumb>}
        {parts[0] && <Crumb last>{PAGE_TITLES[parts[0]] ?? parts[0]}</Crumb>}
      </>
    );
  }
  return (
    <nav aria-label="Breadcrumb" className="min-w-0">
      <ol className="flex min-w-0 items-center gap-1.5 font-data text-[12px] [&>li:first-child>span:first-child]:hidden">
        {crumbs}
      </ol>
    </nav>
  );
}

function ConnectionIndicator() {
  const status = useConnectionStatus();
  if (status === "idle") return null;
  const live = status === "live";
  return (
    <span
      className="hidden items-center gap-1.5 font-data text-[12px] text-muted sm:flex"
      title="Real-time updates"
    >
      <span aria-hidden>{live ? "●" : "◌"}</span>
      {live ? "live" : status === "connecting" ? "connecting…" : "reconnecting…"}
    </span>
  );
}

function ReconnectBar() {
  const status = useConnectionStatus();
  if (status !== "reconnecting") return null;
  return (
    <div role="status" className="border-b border-line bg-raised px-4 py-1 font-data text-[12px] text-muted">
      ◌ Reconnecting… the page keeps polling meanwhile; real-time updates resume on reconnect.
    </div>
  );
}

function TopBar({ user, onMenu, onPalette }: { user: User; onMenu: () => void; onPalette: () => void }) {
  return (
    <header className="flex h-12 shrink-0 items-center gap-3 border-b border-line bg-surface px-3 lg:px-4">
      <Button
        variant="ghost"
        size="sm"
        className="lg:hidden max-md:hidden"
        onClick={onMenu}
        icon={<MenuIcon size={16} aria-hidden />}
      >
        Menu
      </Button>
      <Breadcrumb />
      <div className="ml-auto flex shrink-0 items-center gap-3">
        <ConnectionIndicator />
        <Button
          variant="secondary"
          size="sm"
          onClick={onPalette}
          aria-label="Open the command palette"
          icon={<CommandIcon size={13} aria-hidden />}
        >
          <span className="max-sm:hidden">Search</span>
          <Kbd>⌘K</Kbd>
        </Button>
        <NotificationBell userId={user.id} />
      </div>
    </header>
  );
}

/* ---------------- Help ---------------- */

const SHORTCUTS: [string, string][] = [
  ["⌘K / Ctrl-K", "Command palette"],
  ["g o · g q · g g · g a", "Go to Overview · Queue · Graph · Audit"],
  ["/", "Focus the filter or search"],
  ["j / k · h / l", "Move in lists and the matrix"],
  ["] / [", "Next / previous run that needs you"],
  ["c · d · x", "Confirm · Discard · Cancel (run page; Confirm keeps its confirmation)"],
  ["f · t · w · e", "Logs: full screen · timestamps · wrap · first error"],
  ["?", "This help"],
];

function KeyboardHelp({ open, onOpenChange }: { open: boolean; onOpenChange: (v: boolean) => void }) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        title="Keyboard shortcuts"
        description="Single-key shortcuts never fire while you type, and can be turned off from your user menu."
      >
        <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5">
          {SHORTCUTS.map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="font-data text-[12px]">{k}</dt>
              <dd className="text-muted">{v}</dd>
            </div>
          ))}
        </dl>
      </DialogContent>
    </Dialog>
  );
}

/* ---------------- Routes ---------------- */

function RootRedirect() {
  const { fallback, loading } = useSpaces();
  if (loading) return <PageLoading />;
  if (!fallback)
    return (
      <p className="text-muted">You are not a member of any space yet. Ask an administrator to add you.</p>
    );
  return <Navigate to={`/s/${encodeURIComponent(fallback.name)}`} replace />;
}

/** Old `/stacks/:uuid` links keep working (DESIGN §4.1): redirect to the name-based route. */
function LegacyStackRedirect() {
  const { stackId = "" } = useParams();
  const { list } = useSpaces();
  const stack = useQuery({ queryKey: ["stack", stackId], queryFn: () => stacks.get(stackId), retry: false });
  const space = list.find((s) => s.id === stack.data?.space_id);
  if (stack.isError) return <NotFound />;
  if (!stack.data || !space) return <PageLoading />;
  return (
    <Navigate
      to={`/s/${encodeURIComponent(space.name)}/stacks/${encodeURIComponent(stack.data.name)}`}
      replace
    />
  );
}

function SpaceRedirect({ to }: { to: string }) {
  const { spacePath, loading, current } = useSpaces();
  if (loading || !current) return <PageLoading />;
  return <Navigate to={spacePath(to)} replace />;
}

function UnknownSpace() {
  const { space = "" } = useParams();
  const { list, loading } = useSpaces();
  if (loading) return <PageLoading />;
  return list.some((s) => s.name === space) ? null : (
    <NotFound what={`No space named ${space}, or you are not a member.`} />
  );
}

function NotFound({ what = "This page does not exist." }: { what?: string }) {
  return (
    <div className="flex flex-col items-start gap-2">
      <h1 className="text-title font-semibold">Not found</h1>
      <p className="text-muted">{what}</p>
      <Link to="/" className="link">
        Go to the overview
      </Link>
    </div>
  );
}

function PageLoading() {
  return <p className="font-data text-[12px] text-muted">Loading…</p>;
}

function AppRoutes() {
  return (
    <Routes>
      <Route path="/" element={<RootRedirect />} />
      <Route
        path="/s/:space"
        element={
          <>
            <UnknownSpace />
            <OverviewPage />
          </>
        }
      />
      <Route path="/s/:space/stacks/:stack" element={<StackRoute />} />
      <Route path="/s/:space/stacks/:stack/envs/:env" element={<EnvPage />} />
      <Route path="/s/:space/graph" element={<GraphPage />} />
      <Route path="/s/:space/variable-sets" element={<VariableSetsPage />} />
      <Route path="/runs/:runId" element={<RunPage />} />
      <Route path="/queue" element={<QueuePage />} />
      <Route path="/workers" element={<WorkersPage />} />
      <Route path="/audit" element={<AuditPage />} />
      <Route path="/settings" element={<SettingsPage />} />
      {/* v1 URLs */}
      <Route path="/stacks" element={<SpaceRedirect to="" />} />
      <Route path="/stacks/:stackId" element={<LegacyStackRedirect />} />
      <Route path="/graph" element={<SpaceRedirect to="/graph" />} />
      <Route path="/variable-sets" element={<SpaceRedirect to="/variable-sets" />} />
      <Route path="*" element={<NotFound />} />
    </Routes>
  );
}

/* ---------------- Shell ---------------- */

export function AppShell({ user }: { user: User }) {
  return (
    <SpaceProvider>
      <KeyboardProvider>
        <AnnouncerProvider>
          <TooltipProvider>
            <Shell user={user} />
          </TooltipProvider>
        </AnnouncerProvider>
      </KeyboardProvider>
    </SpaceProvider>
  );
}

function Shell({ user }: { user: User }) {
  const navigate = useNavigate();
  const { spacePath } = useSpaces();
  const [drawer, setDrawer] = useState(false);
  const [palette, setPalette] = useState(false);
  const [help, setHelp] = useState(false);

  useHotkeys({
    "mod+k": () => setPalette((v) => !v),
    "?": () => setHelp(true),
    "g o": () => navigate(spacePath()),
    "g q": () => navigate("/queue"),
    "g g": () => navigate(spacePath("/graph")),
    "g a": () => navigate("/audit"),
  });

  return (
    <div className="flex h-full">
      <a
        href="#content"
        className="sr-only z-50 rounded-base bg-primary px-3 py-2 text-onfill focus:not-sr-only focus:fixed focus:top-2 focus:left-2"
      >
        Go to content
      </a>
      <SideNav user={user} onHelp={() => setHelp(true)} className="max-lg:hidden" />
      <Dialog open={drawer} onOpenChange={setDrawer}>
        <SheetContent title="Menu" side="right" className="w-64 p-0">
          <SideNav
            user={user}
            onHelp={() => setHelp(true)}
            onNavigate={() => setDrawer(false)}
            className="h-full w-full border-r-0"
          />
        </SheetContent>
      </Dialog>
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar user={user} onMenu={() => setDrawer(true)} onPalette={() => setPalette(true)} />
        <ReconnectBar />
        <main id="content" tabIndex={-1} className="min-h-0 flex-1 overflow-auto outline-none">
          <div className="mx-auto w-full max-w-[1440px] p-4 lg:p-6">
            <Suspense fallback={<PageLoading />}>
              <AppRoutes />
            </Suspense>
          </div>
        </main>
        <BottomBar onMenu={() => setDrawer(true)} />
      </div>
      <CommandPalette open={palette} onOpenChange={setPalette} />
      <KeyboardHelp open={help} onOpenChange={setHelp} />
    </div>
  );
}
