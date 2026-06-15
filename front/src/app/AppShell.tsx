import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import {
  Boxes,
  GitGraph,
  ScrollText,
  Server,
  Settings,
  SlidersHorizontal,
  type LucideIcon,
} from "lucide-react";
import { environments, observability, runs, stacks } from "@/api/resources";
import type { User } from "@/api/types";
import { useLogout } from "@/auth/session";
import { NotificationBell } from "@/components/NotificationBell";
import { AuditPage } from "@/pages/AuditPage";
import { GraphPage } from "@/pages/GraphPage";
import { HealthPage } from "@/pages/HealthPage";
import { QueuePage } from "@/pages/QueuePage";
import { RunPage } from "@/pages/RunPage";
import { SettingsPage } from "@/pages/SettingsPage";
import { StackDetailPage } from "@/pages/StackDetailPage";
import { StacksPage } from "@/pages/StacksPage";
import { VariableSetsPage } from "@/pages/VariableSetsPage";

interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
}

const NAV: NavItem[] = [
  { to: "/stacks", label: "Stacks", icon: Boxes },
  { to: "/graph", label: "Graph", icon: GitGraph },
  { to: "/audit", label: "Audit", icon: ScrollText },
  { to: "/workers", label: "Workers", icon: Server },
  { to: "/variable-sets", label: "Variable Sets", icon: SlidersHorizontal },
  { to: "/settings", label: "Settings", icon: Settings },
];

function NavRail() {
  return (
    <nav
      className="flex w-52 shrink-0 flex-col gap-0.5 p-2"
      style={{ borderRight: "1px solid var(--color-border)", backgroundColor: "var(--color-bg-surface)" }}
      aria-label="Primary"
    >
      <div className="mb-3 flex items-center gap-2 px-2 pt-1">
        <Boxes size={18} strokeWidth={1.75} style={{ color: "var(--color-accent)" }} aria-hidden />
        <span className="text-[15px] font-semibold tracking-[-0.01em]">Stackd</span>
      </div>
      {/* Labels are always visible (recognition > recall, a11y) — DESIGN §4. */}
      {NAV.map(({ to, label, icon: Icon }) => (
        <NavLink
          key={to}
          to={to}
          className="flex h-9 items-center gap-2.5 rounded-base px-2.5 text-[13px]"
          style={({ isActive }) => ({
            color: isActive ? "var(--color-accent)" : "var(--color-text-secondary)",
            backgroundColor: isActive ? "var(--color-bg-raised)" : "transparent",
          })}
        >
          <Icon size={17} strokeWidth={1.5} aria-hidden />
          {label}
        </NavLink>
      ))}
    </nav>
  );
}

// Top-level sections that have a real index route — only these segments are linkified (a segment
// like "runs" has no list page, so it stays plain text; the run page carries its own back-link).
const NAVIGABLE_ROOTS = new Set(["stacks", "graph", "audit", "workers", "variable-sets", "settings", "queue"]);

function Crumb({ children }: { children: ReactNode }) {
  return (
    <span className="flex items-center gap-1.5">
      <span aria-hidden>/</span>
      {children}
    </span>
  );
}

// A run has no /runs index, so the generic path breadcrumb would dead-end on "runs". Resolve the
// run's real lineage instead — stack / env are the meaningful (and navigable) parents. Reuses the
// same query keys as RunPage, so it reads from cache with no extra request.
function RunBreadcrumb({ runId }: { runId: string }) {
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
  const linkStyle = { color: "var(--color-text-primary)" };
  return (
    <div className="font-data flex items-center gap-1.5 text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
      <Link to="/stacks" style={{ color: "var(--color-text-secondary)" }}>
        default
      </Link>
      <Crumb>
        {stack.data ? (
          <Link to={`/stacks/${stack.data.id}`} style={linkStyle}>
            {stack.data.name}
          </Link>
        ) : (
          <span>…</span>
        )}
      </Crumb>
      {env.data && (
        <Crumb>
          <Link to={`/stacks/${env.data.stack_id}`} style={linkStyle}>
            {env.data.name}
          </Link>
        </Crumb>
      )}
      <Crumb>
        <span style={linkStyle}>run {runId.slice(0, 8)}</span>
      </Crumb>
    </div>
  );
}

function Breadcrumb() {
  const { pathname } = useLocation();
  const segments = pathname.split("/").filter(Boolean);
  if (segments[0] === "runs" && segments[1]) return <RunBreadcrumb runId={segments[1]} />;
  return (
    <div className="font-data flex items-center gap-1.5 text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
      <Link to="/stacks" style={{ color: "var(--color-text-secondary)" }}>
        default
      </Link>
      {segments.map((s, i) => {
        const path = "/" + segments.slice(0, i + 1).join("/");
        // Linkable when it's a known root, or a child of /stacks (stack detail) — i.e. a real route.
        const linkable = NAVIGABLE_ROOTS.has(s) || (i > 0 && segments[0] === "stacks");
        const isLast = i === segments.length - 1;
        return (
          <span key={path} className="flex items-center gap-1.5">
            <span aria-hidden>/</span>
            {linkable && !isLast ? (
              <Link to={path} style={{ color: "var(--color-text-primary)" }}>
                {s}
              </Link>
            ) : (
              <span style={{ color: "var(--color-text-primary)" }}>{s}</span>
            )}
          </span>
        );
      })}
    </div>
  );
}

function HealthDot() {
  const { data, isError } = useQuery({
    queryKey: ["health"],
    queryFn: observability.health,
    refetchInterval: 5000,
  });
  const ok = !isError && data?.status === "ok";
  const color = ok ? "var(--color-state-finished)" : "var(--color-state-failed)";
  return (
    <Link to="/workers" title="System health" className="flex items-center gap-1.5">
      <span aria-hidden style={{ width: 8, height: 8, borderRadius: 4, backgroundColor: color }} />
      <span className="font-data text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
        {data ? `${data.workers.online}/${data.workers.total} workers` : "…"}
      </span>
    </Link>
  );
}

function TopBar({ user }: { user: User }) {
  const logout = useLogout();
  return (
    <header
      className="flex h-12 items-center justify-between px-4"
      style={{ borderBottom: "1px solid var(--color-border)", backgroundColor: "var(--color-bg-surface)" }}
    >
      <Breadcrumb />
      <div className="flex items-center gap-3">
        <HealthDot />
        <NotificationBell userId={user.id} />
        <span className="font-data text-[12px]" style={{ color: "var(--color-text-secondary)" }}>
          {user.email}
        </span>
        <button
          type="button"
          onClick={() => logout.mutate()}
          className="ui-btn rounded-base px-2 py-1 text-[12px]"
          style={{ border: "1px solid var(--color-border)" }}
        >
          Sign out
        </button>
      </div>
    </header>
  );
}

export function AppShell({ user }: { user: User }) {
  return (
    <div className="flex h-full">
      <NavRail />
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar user={user} />
        <main className="mx-auto w-full max-w-[1440px] flex-1 overflow-auto p-6">
          <Routes>
            <Route path="/" element={<Navigate to="/stacks" replace />} />
            <Route path="/stacks" element={<StacksPage />} />
            <Route path="/stacks/:stackId" element={<StackDetailPage />} />
            <Route path="/runs/:runId" element={<RunPage />} />
            <Route path="/queue" element={<QueuePage />} />
            <Route path="/workers" element={<HealthPage />} />
            <Route path="/audit" element={<AuditPage />} />
            <Route path="/graph" element={<GraphPage />} />
            <Route path="/variable-sets" element={<VariableSetsPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="*" element={<StacksPage />} />
          </Routes>
        </main>
      </div>
    </div>
  );
}
