# DESIGN.md — Front-end design decisions

> Companion to PLAN.md and SPECS.md. This document locks in the design decisions for the interface: visual direction, tokens, navigation, patterns for the key screens. Goal: that every screen built (by a human or by Claude Code) is consistent without re-litigating the choices.

---

## 1. Subject, audience, intent

- **Subject**: a control room for infrastructure. You come here to quickly answer three questions: *what is running?*, *what is waiting on a human decision?*, *who did what?*
- **Audience**: cloud/DevOps/SRE engineers. Expert, daily users, on the keyboard, often on a large screen, sometimes on call on whatever screen is at hand. Zero need for marketing seduction in the app.
- **Emotional intent**: **operational confidence**. The interface of a tool that applies destructive changes to production must be calm, dense, legible, unambiguous. No gamification, no decoration, no gratuitous animation.
- **Anti-references**: the "generic SaaS dashboard" look (floating rounded cards, gradients, 3D illustrations) and the "hacker terminal" look (pure black + acid green). Both are defaults, not choices.

---

## 2. Visual direction

### 2.1 Concept: "operational blueprint"

The aesthetic draws on **technical drawings and engineering schematics**: deep slate-blue backgrounds, fine strokes, monospace labels, hierarchy carried by typography and rules rather than by shadowed boxes. It is the natural universe of the subject (you draw infrastructure) and it ages well.

- **Dark-first**: the dark theme is the reference theme (logs, on-call duty, the habits of the trade). A light theme exists from the start (same token system, never "added later").
- **Deliberately high density**: compact tables, tight line-height on data, generous margins only around decision areas. Density is a service rendered to an expert, not a defect.
- **Flat and delimited by rules**: separations via 1px borders and subtle background variations. No drop shadows, no glassmorphism. Low radius (4 px) — the tool is angular like a schematic.

### 2.2 Signature element: the **phase rail**

THE memorable thing about the interface: a **vertical rail** present on every run page, which materializes the state machine (queued → preparing → planning → checking → unconfirmed → applying → finished; `checking` only appears if checks exist, `confirmed` is transient and is displayed as the start of the applying segment). Each segment:

- semantic color code (cf. §3.2), the active segment animated with a discreet pulse (the only ambient animation in the app);
- clickable: navigates directly to the log section for that phase;
- carries its metadata in mono (duration, exit code, number of checks);
- reused as a **horizontal** miniature everywhere a run is listed (stacks table, audit, run groups) — a visual signature you learn once and re-read everywhere.

The rail IS the product's pedagogy: it makes the state machine visible instead of explaining it.

---

## 3. Design tokens

### 3.1 Palette (dark theme, reference)

| Token | Hex | Usage |
|---|---|---|
| `bg-base` | `#0C1118` | app background (bluish slate, never pure black) |
| `bg-surface` | `#141B24` | panels, tables |
| `bg-raised` | `#1D2733` | interactive elements, hover, sticky headers |
| `border` | `#2A3542` | 1px rules (the main structural material) |
| `text-primary` | `#E8EEF4` | body text |
| `text-secondary` | `#8A97A4` | labels, meta |
| `accent` | `#8B6CF0` (brand violet) | structure: focus, active links/nav, primary CTAs, the mark |
| `decision` | `#E8A838` (signal amber) | the human-decision moment only: Confirm / apply |

**Two accents, two jobs.** The **brand violet** (from the StackD logo) is the structural accent — focus rings, active nav, links, primary CTAs, the wordmark's "D". **Amber** is reserved for the one moment that needs human judgment: the **Confirm/apply** button (and the `unconfirmed` state). Keeping them separate means amber *stands out from the violet chrome* — the most important button in the app is amber, not green ("your judgment is needed", not "all is well"). The green/red spectrum stays free for state semantics.

Light theme: same tokens (`bg-base #F7F9FB`, navy `#1B2733` for text — the logo's own colour, brand violet `#6D3FD4` and darkened amber `#A87A1F` for AA contrast).

### 3.2 State colors (the semantic language, identical in both themes)

| State | Color | Hex (dark) | Where |
|---|---|---|---|
| `queued` / neutral | gray | `#6E7B8B` | badges, rail, graph nodes |
| `running` (preparing/planning/checking/applying) | blue | `#4C8DFF` | + pulse on the active element |
| `unconfirmed` (waiting on a human) | amber | `#E8A838` | the same color as `decision`: *waiting on a human IS the action* |
| `finished` | green | `#3FB950` | |
| `failed` | red | `#F85149` | |
| `discarded` / `canceled` | struck-through gray | `#6E7B8B` | |

`mocked` is not a state but a **modifier** that overlays the current state (a mocked run is also `unconfirmed`, `planning`...) — rendered by a distinct magenta badge `#EC6AB4`, deliberately outside the operational spectrum ("this is not real"): MOCKED badge, mocked values. (Magenta, not purple — purple is now the brand.)

Absolute rule: these colors serve **only** state semantics. Never decorative blue, never green on a button unrelated to a success. This is what makes the app legible at a glance at 3 a.m.

### 3.3 Typography

| Role | Font | Usage |
|---|---|---|
| UI / body | **IBM Plex Sans** | navigation, forms, prose |
| Data / structure | **JetBrains Mono** | ⭐ everything that is *data*: IDs, SHAs, resource names, variable values, durations, eyebrow labels, column headers, logs |
| Display | IBM Plex Sans SemiBold, tracking -1% | page titles, login, empty states |

**"Data is mono" rule**: this is the second identity marker after the rail. A `commit abc1234`, a `vpc-0f3a...`, a `+3 ~1 −0` are always in mono — the eye instantly distinguishes what comes from the system from what comes from the interface. Scale: 13 px base UI (density), 12 px tables and logs, 16/20/24 px titles. No weight beyond SemiBold.

### 3.4 Spacing, radius, elevation

- 4 px grid. Table cell padding: 6×10 px (dense), forms: 12 px.
- Radius: 4 px everywhere, 2 px on badges. No pills except state badges.
- Elevation: no shadows; hierarchy = background (`base < surface < raised`) + rules.
- Iconography: Lucide, 16 px, stroke 1.5, always accompanied by a label (never an icon alone on a destructive action).

---

## 4. Navigation architecture

```
┌──────────┬─────────────────────────────────────────┐
│ Stackd   │ topbar : breadcrumb · health · user      │
│──────────├─────────────────────────────────────────┤
│ ▣ Stacks │                                          │
│ ⎇ Graph  │              content                     │
│ ▤ Audit  │   (max width 1440px, tables full-width)  │
│ ▥ Workers│                                          │
│ ⚙ Sets   │                                          │
└──────────┴─────────────────────────────────────────┘
```

- **Labeled side nav (≈ 208 px, icon + text label)**: Stacks, Graph, Audit, Workers, Variable Sets, Settings, with the `StackD` mark + wordmark at the top. Active item = brand-violet text + `raised` background. The product has few sections: no mega-menu.
  - **Decision (revised): labels are always visible, not only on hover.** An icons-only nav forces *recall* (guessing what an icon means); a labeled nav plays on *recognition* — faster, less ambiguous, and better for accessibility (the label is not carried by the `aria-label` alone). For an expert tool used daily, clarity wins over the 150 px gained. The earlier "56 px icons + tooltip, expandable on hover" track is abandoned: the tooltip hides information behind an interaction.
- **Structural breadcrumb**: `space / stack / environment / run #142` — always present, each segment clickable. It is the backbone of orientation, given the 4-level hierarchy.
- **⌘K command palette** from v1: go to a stack/env, trigger a run, copy an ID. For an expert audience, this is the real primary navigation; the visual nav is the fallback.
- Routing: clean, shareable URLs (`/stacks/core-network/prod/runs/142?phase=plan&line=87`) — a link pasted in Slack during an incident must land exactly at the right place.

---

## 5. Key screens — decisions

### 5.1 `/stacks` — the overview (home)

- **Dense table**, no cards. One row = one stack; columns = environments (dev, staging, prod, in `position` order).
- Each env cell = horizontal mini-rail of the last run + relative timestamp + **Git lag chip `↑3`** (mono, neutral blue-gray — this is information, not a decision: amber stays reserved) when the branch has advanced since the last apply; an attenuated version if the commits do not touch the `project_root`. Tooltip = list of commits, click = trigger a run. Clickable cell → the env.
- Sticky header row, sorting, filter by text and by state ("show me everything that is failed or unconfirmed").
- **"Attention required" zone pinned at the top**: all `unconfirmed` and `failed` runs of the space, across all environments. This is the answer to "what is waiting on a decision?" without searching.

### 5.2 `/runs/{id}` — the most important screen

```
┌──────────────────────────────────────────────────────────┐
│ core-network / prod / run #142        [Confirm] [Discard]│ ← sticky bar
│ abc1234 "add NAT gateway" · J.Dupont · via cascade       │
├────────┬─────────────────────────────────────────────────┤
│ RAIL   │  [Plan ±]  [Checks 2✓ 1⚠]  [Logs]  [Inputs]     │
│ of     │ ┌─────────────────────────────────────────────┐ │
│ phases │ │                 tab content                 │ │
│ (§2.2) │ │                                             │ │
└────────┴─┴─────────────────────────────────────────────┴─┘
```

- **Sticky action bar** at the top, not at the bottom: the decision is always visible, with the context (who triggered, env tier). Confirm button **amber**, disabled with the reason spelled out ("prod tier required — your cap is staging", "approver role required", "destroy permission required", "mocked run — apply disabled", "you triggered this prod run"). The triggerer and the confirmer are shown with their tier.
- **Friction proportional to risk.** An authorized non-prod apply = one click. But confirming an apply on **`tier=prod`** or **any `destroy` run** requires an **explicit confirmation** in a popover anchored to the button: typing the **name of the environment** (GitHub pattern, consistent with deletions §5.7) + a `+a ~c −d` summary of the plan, destructions highlighted. Consistent with the principle: deleting a stack already requires typing its name — applying a destruction on prod cannot be less demanding than editing config. The popover bypasses no access rule (`can_apply` is still evaluated on the API side): it is a misclick safeguard, not a permission check.
- **Staleness banner**: if the tracked branch advances while a plan awaits confirmation, a banner above the content — "Plan computed on `abc1234` · the branch has advanced by 2 commits" — with a *Re-plan* action. Confirming remains possible (applying a precise commit is legitimate), but never unknowingly.
- Header: triggerer AND confirmer shown (Google avatars) — audit is in the interface, not hidden in a page.
- **Plan** tab: `+3 ~1 −0` summary in giant mono, then a list of resources grouped by action (create/update/delete), each resource expandable into an attribute-by-attribute diff (added green / modified amber / deleted red, values in mono). **Destructions are always expanded by default** — a delete is never hidden.
- **Checks** tab: one block per after_plan hook (name, status, duration, output), warn = amber with the note "manual confirmation required".
- **Inputs** tab: resolved variables with their **provenance as a mono badge** (`set:common-aws`, `stack`, `env`, `dependency`, `MOCK` in magenta). Sensitive values: `•••` with no "reveal" button.

### 5.3 Log viewer (in the run page)

- Full screen available (`f`), `bg-base` background, JetBrains Mono 12 px, line-height 1.5.
- Sections collapsible by phase **and by hook** (chevron + duration + exit code in the section header).
- Line numbers in the gutter, click = shareable anchor, highlight of the targeted line on arrival.
- Follow-tail active by default on a run in progress; the slightest scroll up suspends it (floating "↓ Resume following" button with a counter of new lines).
- Full ANSI rendering (palette mapped onto our tokens, not raw terminal colors). Internal `⌘F` search with an occurrence counter. Timestamps toggle. Mandatory virtualization (react-virtuoso).

### 5.4 `/graph` and run groups

- react-flow, dagre layout left→right, **filter by environment name at the top** (see the "prod" graph without the dev noise).
- Node = minimal card: stack/env + mini-rail of the last run. Edge labeled with the number of output references; dashed magenta edge if mocks are involved.
- Run group view: same graph, the nodes color in real time during the cascade. No confetti at the end — a green `finished` state is enough.
- **Mandatory accessible alternative**: a react-flow graph is not navigable by screen reader nor by keyboard alone. A systematic **"list view"** toggle = an adjacency table (`upstream env → downstream env · N references · MOCK?`) that is sortable, focusable, with the same filter. The graph is the default display; the list is the complete functional equivalent, not a stopgap. The graph nodes remain reachable via `Tab` (topological order) with an `aria-label` announcement of the last run's state.
- **Auto-link by name**: a collapsible action wires two stacks in bulk — for every same-named env pair it links each matching upstream output to the downstream input of the same name (one POST instead of adding references one by one). Reports the count of references created.

### 5.5 `/queue` — the execution queue

The visual answer to "why isn't my run starting?".

- Two groups: **In progress** (claimed runs, with worker, active phase and duration) and **Waiting** (`queued` + `confirmed` not claimed), sorted by age.
- Each waiting run displays its **blocking reason computed by the API**, spelled out and in mono: `run #141 active on this environment`, `environment locked`, `no worker compatible with {pool: prod}`, `apply affinity reservation (worker-aws-1, 38s remaining)`.
- Filter by stack/env/pool; direct link to the blocking run and to `/workers`.
- A queue counter in the side nav (badge on the icon) when runs have been waiting for > 2 min — a discreet signal, no toast.

### 5.6 `/audit`

- Dense chronological table: mono timestamp, avatar + email, action as a badge, target as a clickable breadcrumb, summarized context.
- The ⭐ actions (`run.confirmed`, `run.applied`) have a slightly contrasted row.
- Combinable filters in a top bar (actor, action, stack, env, period) reflected in the URL → an investigation is shared by link. Discreet CSV export on the right.

### 5.7 Forms (stack wizard, variables, hooks)

- One column, max 560 px, labels above, help in `text-secondary` under the field.
- Every technical name typed (stack, env, variable) is displayed in mono from the first keystroke.
- Destructive actions require typing the name of the target (GitHub pattern) — not a plain "Are you sure?" modal.
- Wizard: numbered steps because it is a real sequence; each step independently validatable (the repo check runs at step 1, not at the end).
- **Config list items are uniform** (variables, hooks, notifications, secret sources): each is a bordered tile with metadata badges, an **edit** (pencil) and a **delete** (trash) affordance. Editing is in place — the tile becomes an inline form, no separate screen. A sensitive value is never pre-filled (write-only): its edit field stays blank with a "leave blank to keep" hint, so toggling `hcl`/`sensitive` never clobbers the stored secret. Secret sources expose **Rotate token** the same way (write-only new bootstrap credential).

### 5.8 `/stacks/{id}` — stack & environment configuration

- Two top tabs: **Environments** (operate) and **Settings** (configure the stack: general, variables, hooks, notifications, secret sources).
- Each environment row folds its config behind a single **Configure** disclosure (progressive disclosure — one panel at a time), with the daily action (**Plan**) as the sole accent CTA and a discreet **refresh HEAD** icon (force a re-read of the tracked branch from the remote, updating the stale / `↑N` indicators without waiting for the poll).
- The env **Inputs** tab has three sections: **Resolved** (read-only, every value with its provenance badge), **Environment overrides** (editable env-level variables that override the stack-level value of the same name, SPECS §3.4), and **Outputs** (what the env publishes after a successful apply — sensitive ones masked, never a reveal button).

### 5.9 `/workers` — workers & pools

- Workers grouped by pool (read): per-worker status, labels, last heartbeat, agent version, and Diagnostics / Logs actions.
- **Pool management (admin)**: list, create and delete worker pools. Creating one mints the agent registration token, surfaced **once** in a dismissible banner with a copy action — it is hashed at rest and never retrievable again (so the empty-state hint "start an agent with a pool token" is now actionable from the UI).

---

## 6. Real-time patterns

- A single multiplexed WebSocket; TanStack Query remains the source of truth (WS events **invalidate** queries, they do not patch the cache by hand — except logs, which are streamed directly).
- A run's state change is visible everywhere it appears (rail, badges) without a refresh, via subscription to the topics of the displayed entities.
- Discreet connection indicator in the topbar; on a disconnect: a thin "Reconnecting…" banner + `after_seq` catch-up, never a blocking modal.
- No toast for background events (a run that finishes is seen on its rail). Toasts are reserved for feedback on the current user's actions.

---

## 7. Accessibility, states, quality

- Minimum AA contrast on both themes (verified on the state colors over their actual backgrounds).
- **Color is never the sole carrier of state**: each badge has its text label, the rail has icons per segment (✓ ✕ ⏸ ▶). Color blindness = the nominal case in this trade.
- Full keyboard navigation: `j/k` in tables, `f` full-screen logs, `⌘K` palette, visible brand-violet 2 px focus. `prefers-reduced-motion`: the rail's pulse becomes a static opacity change.
- **Skip-link** "Go to content" as the first focusable element (side nav + breadcrumb = many tab stops before the content on a dense app).
- **Polite live regions, never focus stealing**: real-time state changes (rail, badges, "Reconnecting…" banner) are announced via `aria-live="polite"` and never move the user's focus (they may be reading logs or filling out a form). Action toasts use `aria-live` and do not capture focus.
- **Tabular figures**: `font-variant-numeric: tabular-nums` on any data aligned in a column (durations, `+a ~c −d`, counters, timestamps) — JetBrains Mono is already monospaced, but we force tabular-nums so the columns do not dance during streaming.
- **Async action buttons**: `Confirm`/`Discard`/`Trigger`/`Re-plan` go into a `loading` state (spinner + disabled) during the call, to avoid double submission — distinct from the "missing permission" `disabled` which, for its part, carries the reason.
- **On-call mobile journey**: on the two mobile-polished screens (read a run/logs, confirm/discard), touch targets respect a **44 px** minimum; elsewhere desktop density prevails.
- Empty states = invitations to act, in the product's vocabulary: "No stack. Connect a repo to get started." + button. No decorative illustrations.
- Errors: factual, in clear technical English, with the way out ("The webhook was rejected: invalid HMAC signature. Check the secret in Settings → Webhooks."). Never apologies, never vague.
- Skeletons faithful to the final shape for tables; spinners reserved for one-off actions.
- Responsive: optimized for desktop (the trade), but polished mobile consultation for two precise journeys — **reading a run/its logs and confirming/discarding** (on-call). The rest may degrade.

---

## 8. Implementation

- **Tailwind v4 + CSS custom-property tokens** (`--color-bg-base`, `--color-state-running`, ...): the two themes = two sets of variables, components use only the tokens. No hardcoded color in a component.
- **shadcn/ui as the mechanical base** (Dialog, Popover, Command, DropdownMenu) **fully re-skinned** onto our tokens: we keep Radix's accessibility, we replace the default aesthetic (which is precisely the generic look we refuse).
- In-house components (the identity core): `<PhaseRail>` (+ `mini` variant), `<StateBadge>`, `<LogViewer>`, `<PlanDiff>`, `<ProvenanceBadge>`, `<RunActionBar>`, `<EnvCell>`.
- Storybook (or Ladle) from Phase 0 for these components: it is the visual contract that Claude Code must respect when building the pages.
- Self-hosted fonts (IBM Plex Sans, JetBrains Mono) — the app can run on a private network without an external CDN. `font-display: swap`, preload of only the critical weights (Sans 400/600, Mono 400), fallback metrics (`size-adjust`/`ascent-override`) to avoid CLS on load.
- **Theme preference without browser storage** (product invariant: neither localStorage nor sessionStorage). Consequence: the theme follows `prefers-color-scheme` by default; a manual override lives **in memory** (lost on reload) and, if per-user persistence is desired, it is **stored server-side** on the profile (`GET /me`), never in the browser. To be settled when building the topbar (theme toggle).
- Graphs: react-flow + dagre. Logs: react-virtuoso + anser (ANSI). Dates: native `Intl`, relative format < 24 h then absolute local ISO.

---

## 9. What we forbid ourselves (reminder)

- Drop shadows, gradients, glassmorphism, decorative illustrations, emojis in the UI.
- State colors used for anything other than state; brand violet used to carry state meaning; amber used for anything other than the decision moment + `unconfirmed`.
- An icon alone on a destructive action; confirmation without typing the name for deletions **and for any `tier=prod` or `destroy` apply** (§5.2).
- Toasts for background events; unsolicited animations beyond the rail's pulse.
- Hiding or collapsing a resource destruction in a plan.
- Any system text (ID, SHA, value, duration) in the UI font: data is mono, always.
