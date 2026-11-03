import {
  Boxes,
  GitGraph,
  ListOrdered,
  ScrollText,
  Server,
  Settings,
  SlidersHorizontal,
  type LucideIcon,
} from "lucide-react";

// Primary navigation (DESIGN §4). `to` receives the space-path builder: space-scoped pages live under
// `/s/:space`, instance pages do not.

export interface NavItem {
  label: string;
  icon: LucideIcon;
  to: (spacePath: (p?: string) => string) => string;
  /** Path prefixes (after the space) that mark the item active. */
  match: (pathname: string) => boolean;
  adminOnly?: boolean;
  /** One of the four destinations of the mobile bottom bar. */
  mobile?: boolean;
}

const inSpace = (pathname: string, sub: string) => new RegExp(`^/s/[^/]+${sub}`).test(pathname);

export const NAV: NavItem[] = [
  {
    label: "Overview",
    icon: Boxes,
    to: (sp) => sp(),
    match: (p) => /^\/s\/[^/]+\/?$/.test(p) || inSpace(p, "/stacks") || p.startsWith("/runs/"),
    mobile: true,
  },
  { label: "Graph", icon: GitGraph, to: (sp) => sp("/graph"), match: (p) => inSpace(p, "/graph") },
  {
    label: "Queue",
    icon: ListOrdered,
    to: () => "/queue",
    match: (p) => p.startsWith("/queue"),
    mobile: true,
  },
  { label: "Workers", icon: Server, to: () => "/workers", match: (p) => p.startsWith("/workers") },
  {
    label: "Audit",
    icon: ScrollText,
    to: () => "/audit",
    match: (p) => p.startsWith("/audit"),
    adminOnly: true,
  },
  {
    label: "Variable sets",
    icon: SlidersHorizontal,
    to: (sp) => sp("/variable-sets"),
    match: (p) => inSpace(p, "/variable-sets"),
  },
  { label: "Settings", icon: Settings, to: () => "/settings", match: (p) => p.startsWith("/settings") },
];
