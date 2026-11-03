# Front-end redesign — Stage 1: audit and direction

> Status: **validated 2026-10-10** (stage 1 of 4). Decisions (§9): direction A with the two borrowings;
> achromatic chrome; amber narrowed; space and names in the URL; plan diff parsed from the logs until A1;
> stage-3 dependencies approved; API items A1–A10 stay in this document (no issues opened). Stage 2
> output: `docs/DESIGN.md` v2. No application code is changed by this stage.
> Scope: `front/` only. The API, the invariants of CLAUDE.md §4/§8 and the semantic rules listed in the
> brief are treated as fixed. Everything that would need API work is collected in §6, with the SPECS
> section it touches.

**Contents**

1. [Summary](#1-summary)
2. [Method](#2-method)
3. [What fails today, by cause](#3-what-fails-today-by-cause)
4. [Screen by screen](#4-screen-by-screen)
5. [DESIGN.md versus the code](#5-designmd-versus-the-code)
6. [What the API gives us, and what it does not](#6-what-the-api-gives-us-and-what-it-does-not)
7. [Three directions](#7-three-directions)
8. [Recommendation](#8-recommendation)
9. [Decisions I need from you](#9-decisions-i-need-from-you)

---

## 1. Summary

**The front works feature by feature but does not yet tell the truth at a glance.** The audit
found one defect that matters more than all the visual ones: the overview shows each environment's
*most recent run*, not its state, so a production plan waiting for a second approver can read
**CANCELED** on the stack page and the graph (C1). Second, the run page shows the run's state but
not the decision: everyone sees the same enabled Confirm whatever their rights, with no reason, no
triggerer, no confirmer, and an "applied" run looks the same as a "no changes" one (C2).

What is wrong, in order of cost to the user:

1. **Truth** (C1–C3): misleading env summaries, a decision bar without reasons, a three-number plan
   while the full diff sits in the logs, a rail that spins on finished runs and greys out failed
   ones, logs out of chronological order.
2. **Semantics** (C4): amber, green and violet each carry several meanings; five state colours
   fail AA in light theme, `queued` fails in both, the active nav item fails in dark.
3. **Reach** (C5, C6): no responsive layout (the on-call mobile path is unusable), no keyboard, no
   live regions, users land on an empty space, URLs carry no state, Queue unreachable.
4. **Safety in config** (C7): one click disables four-eyes on prod; icon-only and untyped deletes.
5. **Foundations** (C8): the DESIGN.md system was never installed: no fonts, no light theme, no
   Radix, no LogViewer/PlanDiff/RunActionBar/EnvCell, 281 inline styles, lint not configured.

The API already holds most of what the redesign needs (§6): staleness, confirmer and timings, the
text plan, the `after_seq` log protocol, the destroy, cancel and `with_downstream` triggers. Ten
items need API work for an exact version (A1–A10); each has a today-compatible design.

**Recommendation (§8):** Direction A, "Blueprint, finished" — the matrix as home, one page per
entity, a fixed decision bar, the rail as signature — with B's personal decision signal ("needs
you" vs "waiting on someone", next-decision navigation) and C's cascade preview on demand. Plus a
stricter visual language: achromatic chrome, amber reserved to the decision, two outcomes for
`finished`, contrast fixed in both themes, fonts shipped.

Seven questions for you are in §9.

---

## 2. Method

- **Stack.** The dev stack ran in this session's container: Postgres and Garage from
  `deploy/docker-compose.dev.yml`; API, worker (OpenTofu 1.12.2, `runner=local`) and Vite run on
  the host because the container images could not be built behind the session proxy. Same code, same
  seed, same dev login. The OpenTofu registry is blocked from this sandbox, so the fixtures use only
  the built-in `terraform_data` provider.
- **Data.** On top of `task seed` I drove the real API to produce nearly every state the run page can show:
  unconfirmed on dev, staging (mixed `+1 ~1 −1`) and prod (four-eyes, triggered by bob); applied;
  no changes; discarded; canceled; queued behind an active run; mocked (`used_mocks=true`); failed in
  tofu (undeclared variable on a feature branch); failed in an `after_plan` platform hook; destroy
  awaiting confirmation; dependency cascade; a drift check that found drift; stale envs after
  `task push-change`; comments. Not produced: a running apply (transient), a run group (no
  `with_downstream` UI to trigger it, and nothing exposes groups, A5).
- **Screenshots.** Playwright (`chromium`), 1440 × 900 and 390 × 844, full page, for admin, and for
  alice and bob where rights differ (prod four-eyes run, mocked run, audit, notifications). 28
  views × 2 widths × 2 themes = **112 images** in `screenshots/before/`, named
  `<nn>-<view>--<width>-<theme>.png`. The light theme cannot be selected in the UI (C8): those
  images set `data-theme="light"` on `<html>` from the harness.
- **Code.** Read all of `front/src` (pages, components, `api/*`, stories, tests), the e2e smoke test
  and the relevant API routers/schemas to check what data exists. Contrast ratios computed with the
  WCAG 2.x formula from the token values in `front/src/styles/tokens.css`.
- **Checks at baseline.** `pnpm typecheck` passes; `pnpm test` passes (4 files, 19 tests);
  `pnpm lint` fails (no ESLint config/dependency).

---

## 3. What fails today, by cause

Grouped by root cause rather than by screen. Each cause lists its symptoms with the screenshot that
shows them (`before/<name>--<width>-<theme>.png`). Fixing a cause fixes every symptom under it.

### C1. The front shows *the latest run*, not *the state of the environment*

Every place that summarises an environment (stack page, graph node) shows `runs.list(env)[0]`: the
most recently created run, whatever its type. Three different things are mixed into one badge:
tracked runs, proposed (PR) runs and drift-check runs.

- `demo-network/prod` reads **CANCELED** while an unconfirmed prod plan waits for a second pair of
  eyes. A queued run was created and cancelled after it, so it became "latest" and hid the pending
  decision (`04-stack-network--1440-dark`, `30-graph--1440-dark`).
- `demo-app/prod` reads **QUEUED** while a mocked plan blocks it (`30-graph`).
- `demo-app/dev` reads **FAILED**: the failure is a *proposed* run on a broken feature branch; the
  environment itself was applied successfully (`30-graph`).
- `demo-app/staging` reads **FINISHED**: that is the drift check, which found drift
  (`drift_status=drifted`). The DRIFT chip exists on the stack page but not on the graph.
- Staleness (`stale=true` on network/dev and network/staging) appears nowhere.

This is the single most dangerous defect for the 3 a.m. user: the overview can say "nothing to do"
while a production decision is pending. An env summary needs to be **composed**: the active run (if
any, which is unique per env, SPECS §3.5) first, else the last tracked outcome, with drift,
staleness, lock and mocks as separate modifiers. Proposed and drift runs never stand for the env.

### C2. The run page shows state, not the decision

The run page answers "what state is it in" and leaves "can I act, and should I" to guesswork.

- **Confirm looks identical for everyone.** Bob (writer, who triggered the prod run, cannot confirm
  on two counts) sees the same enabled amber *Confirm* as an admin (`12-…-bob` vs `11-…-admin`). He
  learns the rule from a 403 after clicking. Alice on the mocked run gets an enabled Confirm and a
  409 (`17-run-mocked`). No reason is ever shown, though everything needed is in the run, the env,
  the tier catalog and `/auth/me` (§6).
- **The four-eyes status is invisible.** Neither the triggerer nor the confirmer is displayed;
  `trigger_user_id`/`confirmed_by_user_id` are never rendered. "Who may still confirm" is unknowable.
- **Disabled actions on terminal runs.** A failed or finished run still shows Confirm (amber at 50 %
  opacity, which reads as a brown *enabled* button) and Discard
  (`19-run-failed-hook`, `15-run-finished-nochanges`). Cancel is never offered, although the API
  supports it on `queued`/`unconfirmed`.
- **"Finished" is ambiguous** (gap 4). The no-change run (`15`) and the applied run (`14`) look the
  same: green FINISHED, rail "Apply ✓". The no-change run never applied anything.
- **The rail lies in two cases.** On a finished run the last segment shows a blue spinner
  ("active": `ORDER.finished === at`, `RunPage.tsx:46`), so a finished run looks like it is still
  working (`15`). On a failed run every segment turns grey with a ▷ icon (`19`): it reads as "not
  started", and *where* it failed is only in an error line that says `after_plan`.
- **The plan is three numbers.** `+1 ~1 −1` with no resource names, while the full resource diff is
  sitting in the logs a few hundred lines below (`10-run-unconfirmed-staging-mixed`). The destroy
  confirmation says "3 resource(s) will be destroyed" without naming them (`20-run-destroy`).
  `+4 ~0 −0` colours the zeros too, so a reader scans three coloured numbers for one fact.
- **"No plan yet" on a run whose plan ran** and was rejected by a check (`19`).
- **No staleness banner** although network/staging is stale with an unconfirmed plan on it (`10`).
- **No time anywhere.** No created/finished timestamps, no durations, no relative age.

### C3. Logs are a dump, and partly out of order

- The API sorts chunks by `(phase, seq)`; `phase` is a string, so **`checking` sorts before
  `planning`**: the after_plan hook output appears *above* the plan it checked (`19-run-failed-hook`).
  The front concatenates without re-ordering. (The API ordering is a bug to report; the viewer must
  order by time — line `t` — regardless.)
- The phase/section name is prefixed on every line (`planning planning planning…`), eating 9–30
  characters of a 12 px mono line; blank tofu lines become "planning" alone.
- 400 px tall box inside a long page: the log is the thing an on-call reader wants most and it gets
  the least room. No line numbers, anchors, search, follow-tail, full screen, sections, or jump to
  the first error. Every line is a React node (no virtualization).
- Every WebSocket signal re-downloads the whole log (and the run and the comments), although the
  signal carries `phase` and `max_seq` and the endpoint accepts `after_seq`.

### C4. The colour system has drifted from "one hue, one meaning"

Measured contrast (WCAG 2.x) of the current tokens, text on its real background:

| Token | Dark on base / surface / raised | Light on base / surface / raised | Verdict |
|---|---|---|---|
| `state-queued` | 4.39 / 4.02 / 3.50 | 4.09 / 4.31 / 3.83 | **fails AA** both themes (used for 12 px badge text: QUEUED, DISCARDED, CANCELED) |
| `accent` (violet) | 4.96 / 4.54 / **3.96** | 6.01 / 6.34 / 5.64 | **fails on raised** in dark = the active nav item |
| `state-running` | 5.92 / 5.41 / 4.72 | **4.39** / 4.63 / **4.12** | fails in light |
| `state-unconfirmed` = `decision` | 9.10 / 8.32 / 7.26 | **3.64 / 3.84 / 3.41** | **fails in light** — DESIGN §3.1 claims AA |
| `state-finished` | 7.45 / 6.82 / 5.95 | **3.05 / 3.22 / 2.86** | **fails in light** |
| `state-failed` | 5.65 / 5.17 / 4.51 | 5.07 / 5.36 / 4.76 | passes (barely on raised) |
| `mock` | 6.57 / 6.01 / 5.24 | 4.58 / 4.84 / **4.30** | fails on raised in light |
| `border` (inputs) | 1.52 / 1.39 / 1.21 | 1.34 / 1.41 / 1.25 | **fails 3:1** non-text contrast for form controls |

Disabled controls use `opacity: 0.5`, which halves all of the above.

Hue collisions:

- **Amber** means "needs a human" (DESIGN §3.1) but also paints the **drift** chip (SPECS §19.5 says
  neutral), the plan `~`, the "open thread" counter and FALLBACK. In `04-stack-network`, an amber
  UNCONFIRMED and an amber DRIFT read as the same urgency.
- **Green** means `finished`, but also "HTTP 2xx" and "online" on the workers page
  (`41-workers`), and the plan's `+`.
- **Violet** (brand) fills primary buttons ("Plan", "New stack", "Create pool"), links, the active
  nav, the notification badge and log event names (`41-workers`). It sits between running-blue and
  mocked-magenta; the brand is the loudest colour on most screens, louder than state.
- **Magenta** on the graph marks every edge that *has a mock defined* (`has_mock`), not edges that
  *served* a mock: in the demo every edge is dashed magenta (`30-graph`), so the "not real" signal is
  saturated and means nothing.
- **Telemetry noise**: the workers page counts 46 "Warnings+errors", nearly all of them the worker's
  25 s long-poll `jobs/claim` flagged `(slow)`, which is its normal behaviour (`41-workers`).

### C5. No responsive layout, no keyboard, no live regions

- At 390 px the side nav keeps its 208 px, the topbar overflows to ~840 px, and the run page is
  clipped mid-word with the Confirm card off-screen (`11-…-admin--390-dark`). The on-call mobile
  journey (DESIGN §7: read a run, confirm or discard) is unusable.
- No skip link, no shortcuts, no `aria-live` region (state changes are silent for screen readers),
  no visible focus style on most controls, the walkthrough dialog has no focus trap, the
  notification popover is not a Radix popover (no Escape, no focus return).
- The graph's React Flow controls and edge labels keep the library's white default skin in dark
  theme (`30-graph`), and nodes have no accessible name.

### C6. Orientation: space, URL and navigation

- **The home opens on an empty space.** The space list is ordered by name and the first one wins:
  admin, alice and bob land on `default` — "No stacks yet" — while all the demo data is in `demo`
  (`02-stacks-default-space`). The switcher is a small select in the topbar.
- The space is in-memory: a reload, a new tab or a shared link always reopens `default`. The
  breadcrumb's first segment shows the *selected* space, not the entity's (`10`: "default /
  demo-network / …" for a stack that lives in `demo`).
- Stack URLs are UUIDs and the breadcrumb prints them raw
  (`default / stacks / 01a12765-e228-…`, `04`).
- `/queue` is not reachable from the UI. Audit is in everyone's nav but admin-only: bob gets an empty
  table with no message (`51-audit-bob`).
- An environment has no page and **no run history**: from the UI you reach only its latest run
  (C1). Older runs are reachable only from a notification or a pasted URL.
- Notifications say "Run failed" / "Run finished" without naming the stack or environment (only
  approval requests carry the env), with locale timestamps (`62-notifications`).

### C7. Configuration surfaces: friction where none is needed, none where it is

- A single click on the **four-eyes** checkbox of the `prod` tier saves immediately: the strongest
  safety switch in the product has less friction than renaming a stack (`61-settings`). Same for a
  user's allowed tiers and role.
- **Icon-only destructive actions** (DESIGN §9 forbids them): tier delete is a red `×`
  (`61-settings`), config items use a bare trash icon; pool delete, variable-set delete, hook delete
  and **state force-unlock** are one click without typing the name, and force-unlock is offered to
  non-admins (the API refuses them afterwards).
- The environment card folds **ten tabs** (Inputs, Dependencies, Hooks, Notifications, Cloud,
  State, Command, Promote, Settings, Danger) behind "Configure" (`05-stack-network-configure`): daily
  actions (Promote, Command, the run history that does not exist) sit next to rare configuration.
- The destroy run type, `with_downstream` and Cancel exist in the API with no UI (§6).

### C8. The design system exists on paper only

- Fonts are not shipped (system fonts render, `03`). Light theme unreachable without devtools: every
  `*-light.png` was produced by setting `data-theme="light"` from the test harness.
- No primitives library; 281 inline `style=` attributes; `ui.tsx` re-implements Button, Tabs,
  Checkbox, Card with inline colour objects. The same table markup (header row, cell padding, border)
  is re-written in nine files.
- Pages mix data fetching, layout and behaviour: `StackDetailPage.tsx` 654 lines, `GraphPage.tsx`
  394, `HealthPage.tsx` 381, `RunPage.tsx` 338. Per-env queries poll every 4 s on the stack page and
  every 30 s per graph node, on top of WS invalidation.
- `pnpm lint` does not run (no ESLint dependency or config).

---

## 4. Screen by screen

One line per screen: what it is for, what fails, which causes. Screenshots for every row exist at
1440 and 390 px, dark and light, in `screenshots/before/`.

| Screen | Screenshot | Job | What fails | Causes |
|---|---|---|---|---|
| Login | `01-login` | Get in | Fine on desktop. Dev personas show email and role but not tiers, so the three personas look alike. | C8 |
| Stacks (home) | `02-stacks-default-space`, `03-stacks-demo-space` | "What is running, what waits on me?" | Lands on an empty space; once in `demo`, a list of name / repo URL / tool. Nothing about environments, runs, decisions, drift, staleness. | C1, C6 |
| Stack detail | `04-stack-network`, `06-stack-app` | Operate a stack's envs | Env cards show the latest run (prod reads CANCELED with a decision pending); no stale/lag; no run history; one violet *Plan* per row competes with the amber state; breadcrumb shows the UUID. | C1, C4, C6 |
| Env configure | `05-stack-network-configure` | Configure an env | Ten tabs mixing daily actions and rare config; Danger as a tab; full-width form row for a variable. | C7 |
| Run · unconfirmed | `10-run-unconfirmed-staging-mixed` | Decide | Counts only; plan buried in a 400 px log; no stale banner (env is stale); no who/when; Confirm not sticky. | C2, C3 |
| Run · prod four-eyes | `11-…-admin` (confirm open), `12-…-bob`, `13-…-alice` | Decide under four-eyes | Three users with three different rights see the same buttons; triggerer not shown; confirm card pushes content down and does not list what is destroyed or created. | C2 |
| Run · destroy | `20-run-destroy-unconfirmed` | Decide a destruction | Typed name works; resources being destroyed are not named; the disabled "Confirm destroy" is a brown block that reads as enabled. | C2, C4 |
| Run · mocked | `17-run-mocked` | Understand why apply is blocked | MOCKED badge and MOCK provenance are clear; Confirm stays enabled and fails with a 409; `allow_mock_apply` is not mentioned. | C2 |
| Run · finished | `14-run-finished-applied`, `15-run-finished-nochanges` | Know what happened | Applied and no-change are identical; the rail's last segment spins blue on a finished run; no confirmer, no duration; Confirm/Discard still displayed. | C2 |
| Run · failed | `18-run-failed-tofu`, `19-run-failed-hook` | Know why it failed | Rail all grey; error is the bare word `after_plan`; "No plan yet" on a run whose plan ran; hook output above the plan it checked; no jump to the first error. | C2, C3 |
| Run · queued / cascade / discarded | `21-run-queued`, `22-run-cascade`, `16-run-discarded` | Know why it waits / where it came from | Queued run does not say what it waits for (the queue API knows: "another run active"); cascade run says "via dependency" with no link to the upstream run or env. | C2, C6 |
| Graph | `30-graph`, `31-graph-list` | See dependencies and cascades | Latest-run badges mislead (C1); every edge magenta; edges say "1 ref" instead of the output; node click goes to the stack; React Flow controls/labels un-themed; list view ignores the filter. | C1, C4, C5 |
| Queue | `40-queue` | "Why isn't my run starting?" | Unreachable from nav; raw IDs for run and worker, no stack/env; lists unconfirmed runs with a worker ID as if they were executing. | C6 |
| Workers & health | `41-workers` | Is the platform healthy? | "46 warnings+errors" is mostly normal long-poll; green and violet used for telemetry; pool delete one-click. | C4, C7 |
| Audit | `50-audit-admin`, `51-audit-bob` | Who did what | Dominated by `auth.login`; target is only a kind ("run"), no link, no context; locale timestamps; one free-text filter not in the URL; empty and unexplained for non-admins. | C6 |
| Variable sets | `60-variable-sets` | Factor config | Readable; Delete next to Configure with equal weight and no typed name; attachments hidden behind Configure. | C7 |
| Settings | `61-settings` | Tiers, users | Four-eyes on prod toggles with one click; tier delete is an icon-only `×`; role and tier grants save on change with no summary of what changes. | C7 |
| Notifications | `62-notifications` | Inbox | Items do not name the env; locale timestamps; violet badge. | C4, C6 |

At 390 px every screen has the same failure (C5): fixed side nav, overflowing topbar, clipped content.

---

## 5. DESIGN.md versus the code

What DESIGN.md decides, and what the code does. "Missing" means no trace in `front/src`.

| DESIGN.md | Decision | In the code |
|---|---|---|
| §2.2 | Phase rail: clickable segments, per-segment metadata (duration, exit code, checks), mini variant everywhere a run is listed | Rendered on the run page only, **not clickable** (no `onSelect` passed), **no metadata** (no `meta` passed). A failed/discarded run paints every segment grey ("skipped"): the rail never shows *where* it failed although `failed` exists in `PhaseStatus`. The mini variant is used nowhere outside Ladle. |
| §3.1 | Dark-first **and** a light theme "from the start" | Light tokens exist but are **unreachable**: `index.html` hard-codes `data-theme="dark"`, nothing reads `prefers-color-scheme`, no toggle. |
| §3.1 | Light theme: "darkened amber `#A87A1F` for AA" | Measured 3.64:1 on `bg-base`, 3.41:1 on `bg-raised`: **fails AA** for the 12 px badge text it is used on (see §3, C4). |
| §3.3 | IBM Plex Sans + JetBrains Mono, self-hosted (§8) | **Not shipped.** No `@font-face`, no font files: the app renders in `system-ui` and `ui-monospace`. "Data is mono" holds only because the fallback is a monospace. |
| §4 | ⌘K command palette "from v1, the real primary navigation" | Missing. |
| §4 | Breadcrumb `space / stack / env / run #142`, every segment clickable | Generic path splitter: shows raw segments, so a stack page reads `default / stacks / 0199…-uuid`. The space segment is the *selected* space, not the entity's. Only the run page resolves real names. |
| §4 | Shareable URLs that land on the phase and log line | Routes carry IDs only. No query state anywhere (`useSearchParams` is never used): tabs, filters, expanded panels, log position are lost on reload or share. The active space is in memory, so a shared link opens in the first space of the list. |
| §5.1 | Home = stack × env matrix, mini-rail per cell, `↑N` lag chip, sticky header, sort, text+state filter, pinned "Attention required" zone | Home is a three-column list (`NAME · REPO · TOOL`). **No matrix, no env, no run state, no attention zone, no lag chip, no filter.** |
| §5.2 | Sticky action bar; Confirm disabled *with the reason spelled out*; triggerer and confirmer shown with their tier | Bar is not sticky. Confirm/Discard are disabled without any reason. Triggerer and confirmer are never shown (`trigger_user_id`, `confirmed_by_user_id` unused). |
| §5.2 | Friction: typed env name for four-eyes tier / destroy, in a popover anchored to the button | Implemented as an inline card that pushes the page down (not anchored); the typed name is required as specified. Also triggered by `protected` and `require_second_pair_of_eyes` (matches SPECS §4.2, broader than DESIGN §5.2 — DESIGN should be updated). |
| §5.2 | Staleness banner with *Re-plan* | Missing (PLAN Phase 5 already records it as not in the UI). |
| §5.2 | Plan tab: big `+a ~c −d`, resources grouped by action, attribute diff, **destroys expanded by default** | Counts only. The per-resource diff exists in the logs (`section="plan"`, the worker streams `tofu show -no-color`) but is shown only as raw log lines. |
| §5.2 | Tabs Plan / Checks / Logs / Inputs | One long scrolling column of cards, no tabs. |
| §5.3 | Log viewer: virtualized, sections per phase and hook, line numbers + anchors, follow-tail, ⌘F search, timestamps toggle, full screen `f` | A `max-height: 400px` div rendering every line; the section/phase name is repeated as a prefix on **every line**; no line numbers, anchors, search, follow, full screen; no virtualization (`react-virtuoso` not installed); custom 8-colour ANSI parser (`anser` not installed). |
| §5.4 | Graph: node = stack/env + mini-rail; edge labelled with reference count, dashed magenta for mocks; Tab-reachable nodes with `aria-label`; list view as full equivalent; run-group view | Nodes show a full `StateBadge`; clicking a node opens the **stack**, not the env. Edges carry "N refs". No `aria-label` on nodes. The list view ignores the filter. No run-group view (the API does not expose groups, §6). |
| §5.5 | Queue page with blocking reasons, nav counter when runs wait > 2 min | Page exists at `/queue` but is **not in the nav** and not linked from anywhere; rows show raw run ID / state / worker ID; no stack/env names; no counter. |
| §5.6 | Audit: filters (actor, action, stack, env, period) in the URL; ⭐ rows contrasted; target as breadcrumb | One free-text action filter, not in the URL. Target shows only `target_kind`. The endpoint is admin-only (SPECS §2.4 residual) yet the nav shows Audit to every role, who get an empty table and no explanation. |
| §5.7 | Destructive actions require typing the name | Environment delete: yes. **Force unlock (state)**: one click, no confirmation, shown to non-admins. Pool delete, variable delete, hook delete: one click. |
| §6 | Discreet connection indicator; "Reconnecting…" banner; catch-up via `after_seq` | No connection indicator, no banner. Every WS signal re-fetches the **whole** log, the run and the comments. |
| §7 | Skip link, `aria-live="polite"` for real-time changes, `j/k`, `f`, ⌘K, brand-violet focus ring | No skip link; **0** `aria-live` regions; no keyboard shortcuts; only the checkbox has a custom focus style (others fall back to the browser outline, which is near-invisible on the dark surfaces). |
| §7 | 44 px targets and polished mobile for "read a run / confirm or discard" | No responsive layout: at 390 px the 208 px side nav stays, the topbar overflows horizontally, the run page rail takes 192 px. |
| §8 | shadcn/ui (Radix) re-skinned: Dialog, Popover, Command, DropdownMenu | Not installed. The Walkthrough is a hand-rolled `role="dialog"` with no focus trap; the notification bell is a hand-rolled popover. |
| §8 | Identity components `PhaseRail`, `StateBadge`, `LogViewer`, `PlanDiff`, `ProvenanceBadge`, `RunActionBar`, `EnvCell`, each with a story | Only `PhaseRail`, `StateBadge`, `ProvenanceBadge` exist (with stories). `LogViewer`, `PlanDiff`, `RunActionBar`, `EnvCell` do not exist. |
| §8 | No hard-coded colour | Holds: no hex/rgb in components, colours come from tokens. But **281 inline `style=` attributes** across 36 files carry them (the brief's ~110 undercounts multi-line ones), so tokens are applied by hand everywhere and Tailwind utilities (`bg-surface`, `text-muted`…), although mapped in `index.css`, are almost never used. |
| §9 | Amber only for "needs a human" | Amber also paints the **drift** chip (SPECS §19.5 says *neutral*), the plan diff `~`, the "open threads" counter, and the FALLBACK modifier. See C4. |

Tooling gap that blocks the brief's definition of done: **`pnpm lint` fails** — `eslint` is not a
dependency and there is no `eslint.config.js` (the script resolves some global ESLint 10 that finds
no config). There is no formatter config either. `pnpm typecheck` and `pnpm test` (4 files, 19 tests)
pass. Stage 3 has to add ESLint (flat config, `typescript-eslint`, `react-hooks`) and Prettier first.

---

## 6. What the API gives us, and what it does not

The redesign is front-only, so I checked what each idea can stand on. Three findings matter more than
the rest.

**a. More is available than the front uses.** These exist in the API today and the front ignores them:

| Data / endpoint | Where | Use in the redesign |
|---|---|---|
| `RunOut.confirmed_at`, `claimed_at`, `commit_author`, `worker_id`, `command` | `api/app/runs/schemas.py` (missing from `front/src/api/types.ts`) | "Applied" vs "No changes" (`finished` + `confirmed_at`), who/when on the action bar, worker on the run page |
| `trigger_user_id`, `confirmed_by_user_id` + `GET /users/mentionable` (open to all) | already typed, never rendered | Four-eyes status: "triggered by bob · confirmer must be someone else" |
| `Environment.stale`, `commits_ahead`, `affects_project_root`, `head_sha`, `locked`, `drift_run_id` | typed, never rendered | `↑N` chip (attenuated when `affects_project_root=false`), stale-plan banner, lock indicator, link to the drift run |
| `POST /environments/{id}/runs {type: "destroy"}` | `runs.trigger` accepts it, no caller | A destroy trigger (with `can_destroy` gating and the typed-name friction) |
| `POST /environments/{id}/runs {with_downstream: true}` | not in the client | "Plan with downstream" (starts a run group, SPECS §9.4) |
| `POST /runs/{id}/cancel` | in the client, no caller | Cancel a `queued` / `unconfirmed` run (409 while a worker executes it, SPECS §4.1) |
| `GET /runs/{id}/logs?phase=&after_seq=` | the client always fetches everything | Incremental log streaming: the WS signal carries `{kind, run_id, phase, max_seq}` (SPECS §5.3), so the viewer can fetch only the new chunks instead of the whole log |
| Log line timestamps `lines[].t` | rendered nowhere | Per-phase durations on the rail (first/last line per phase), timestamps toggle |
| Plan text in the logs: `phase="planning"`, `section="plan"` = `tofu show -no-color` of the saved plan (`worker/agent/main.py`) | shown raw | A parsed, grouped `PlanDiff` (see b.) |
| `GET /graph` edges with `policy` | rendered as a table | Cascade preview before Confirm: BFS over edges from the env (§8) |

**b. The structured plan is not served, but the text plan is.** `GET /runs/{id}/plan` returns only
`{plan_summary, used_mocks}` (`api/app/runs/router.py:169`); the masked `plan.json` artifact is
reachable only from the worker API. The human-readable plan is in the logs, one resource block per
`  # <address> will be created | updated in-place | destroyed | must be replaced`, with `+ ~ - -/+`
attribute lines. A front-side parser of that format gives everything the brief asks for on the Plan
tab (destroys and replaces first, tag-only changes detected by attribute name, search, counts) with
today's data. It is a text format, so it is a stopgap: the parser must fall back to the raw log
block on anything it does not recognise, and never drop a line (a destroy is never hidden).

**c. Permissions are evaluated on instance defaults only.** `GET /auth/me` returns the user's
*instance* `role`/`allowed_tiers`/`can_destroy`; the effective values are per space (SPECS §2.4,
Phase F) and `GET /spaces/{id}/members` is space-admin only. So a front-computed reason ("tier prod
not in your allowed tiers") can be wrong for a user whose membership differs from their defaults.
The redesign computes the reason client-side for display, labels it as such when it cannot be sure,
and always surfaces the API's own 403 `detail` verbatim when an action is refused.

### Needs API work

Ideas from the brief that cannot be done exactly with today's data. Each has a today-compatible
version in the design; the API change would make it exact or cheaper.

| # | Need | Today-compatible version | SPECS section it would touch |
|---|---|---|---|
| A1 | Structured plan: `resource_changes` from the masked `plan.json` on `GET /runs/{id}/plan` | Parse the `plan` log section (b.) | §5.1 (masked artifact), §7.3, §12 Runs |
| A2 | The caller's effective permission on a run: `{can_confirm, can_discard, can_cancel, reasons[]}` (or the effective membership for the current space) | Compute from `/auth/me` + env tier + four-eyes rules; show API 403 detail on refusal | §2.4, §4.2, §12 |
| A3 | Who may still confirm (eligible approvers for an env) | State the rule, not names: "an approver with tier `prod`, other than bob" | §2.4, §17 (approval_request already targets them) |
| A4 | Space-wide run query (`GET /runs?space=&state=unconfirmed,failed&limit=`) and/or `last_run` on `EnvironmentOut` | Matrix and attention zone fan out: `stacks` → `environments` per stack → `runs` per env (1 + S + E requests, the per-env runs list is unpaginated) | §12 Runs / Stacks & environments |
| A5 | `run_group_id`, `parent_run_id` on `RunOut` + `GET /run-groups/{id}` | No run-group view; a cascade run shows "via dependency" and links to the upstream env, not the parent run | §3.5, §9.4 |
| A6 | Compute `commits_ahead` (never set today, see below) and list the commits between applied and head for the tooltip | Binary "behind" chip from `stale` (`↑`, no number), tooltip with `head_sha` | §9.6 |
| A7 | Run events (`GET /runs/{id}/events`, from `run_events`) for exact phase timings and the transition timeline | Durations from log timestamps; "—" when a phase logged nothing | §3.6, §12 |
| A8 | Theme preference on the profile (`PATCH /auth/me`) | `prefers-color-scheme` + in-memory override (lost on reload), as DESIGN §8 allows | §2.2 users, §12 Auth |
| A9 | Audit readable by non-admins, scoped to their spaces | Hide Audit from non-admins and say why on direct access | §2.4 residual, §6.4 |
| A10 | Log download (`GET /runs/{id}/logs/download`) | Client-side "save as text" from what is loaded | §5.2 |
| A11 | Queue entries with `created_at`, `claimed_at`, the worker name and, for `active_run`, the id of the run holding the env; compute `no_compatible_worker` | One `runs.get` per queue row; the holder is looked up among the queue entries; "no worker online" inferred from `/health` (added in stage 4) | §7.2, §12 Workers |
| A12 | Dev personas with their `allowed_tiers` on `GET /auth/dev/personas` (dev-only) | Login lists email and role only (added in stage 4) | DEV.md §dev login, §2.4 |
| A13 | Notification context naming the stack and env for `run_finished` / `run_failed` | One `runs.get` per item while the popover is open, names from the stack/env lists (added in stage 4) | §17 |
| A14 | Audit paging cursor (`before=<event id>`) | `to` bound on the oldest loaded `created_at`, duplicates dropped (added in stage 4) | §6.4, §12 Audit |

None of these is required to start Stage 3; A1, A2 and A4 are the ones whose stopgaps cost the most
(text parsing, possibly-wrong reasons, request fan-out on the home page with many stacks).

### API defects found along the way

Not design work, but they affect what the front can show; reported here rather than fixed (this is a
front-only redesign):

- `GET /runs/{id}/logs` orders by `(phase, seq)` with `phase` a string, so `checking` chunks come
  before `planning` ones (`api/app/runs/router.py:165`). The viewer will order by line time
  regardless.
- `Environment.commits_ahead` is never computed (only cleared, `api/app/environments/router.py:334`),
  so the `↑N` chip can only be the binary "behind" variant SPECS §9.6 allows as a fallback (A6).
- A run that fails at an `after_plan` check reports `error = "after_plan"` (the stage name) and no
  `plan_summary`, although the plan ran.
- `GET /queue` reports `apply_affinity_hold` for every `confirmed` run, also after the affinity
  window has passed (`api/app/workers/pools_router.py:146`); `no_compatible_worker` is never
  produced. Found in stage 4.
- A failed `command` run is audited as `run.apply_failed`. Found in stage 4.

---

## 7. Three directions

Three directions that differ in what the product puts first, not only in looks. All three keep the
fixed rules (no browser storage, tokens only, WS invalidates, typed-name friction, destroys never
collapsed, sensitive never revealable, colour never alone, amber = needs a human, mocked = magenta
modifier) and all three ship the same identity components. The wireframes use the seeded demo data.

Legend for the wireframes: `▰` done segment, `▱` pending, `◐` running, `⏸` waiting on a human,
`✕` failed, `✓` finished with changes applied, `=` finished with no changes, `◆` mocked modifier,
`↑3` branch 3 commits ahead (a bare `↑` until A6), `≈` drift.

### Direction A — "Blueprint, finished"

**Idea.** Build DESIGN.md as written, then fix what it gets wrong: the matrix is home, every entity
has its own page, the decision bar is a fixed band, the phase rail is the signature.

**Navigation.** Labelled side nav (Overview, Graph, Queue, Workers, Audit, Variable sets, Settings),
labels always visible as DESIGN §4 decided; below 1024 px it becomes a drawer, below 768 px a bottom
bar with the four on-call destinations. Breadcrumb in the top
bar built from entities, not the path. ⌘K for jumping and acting. New routes:
`/` (matrix), `/s/:space/stacks/:stack`, `/s/:space/stacks/:stack/envs/:env` (env page with run
history), `/runs/:id?tab=plan&line=planning:87`. The space goes into the URL.

**Matrix (home).**

```
Overview · demo                          [⌘K]  filter: [ app_______ ] [state: needs attention ▾] [tier ▾]
┌ NEEDS ATTENTION · 4 ─────────────────────────────────────────────────────────────────────────────┐
│ ⏸ demo-app / prod      AWAITING DECISION  +2 ~1 −0   bob · 12m   you can't confirm: you triggered │
│ ✕ demo-network / dev   FAILED at planning  exit 1    4m ago                                       │
│ ◆ demo-app / dev       MOCKED · apply blocked (allow_mock_apply off)                              │
│ ≈ demo-network / prod  DRIFT  checked 2h ago                                                      │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
 STACK ▾          dev                       staging                   prod  ⓘ four-eyes
 ──────────────── ───────────────────────── ───────────────────────── ─────────────────────────
 demo-network     ▰▰▰▰ = 2h        ↑3       —                         ▰▰▰▰ ✓ +1 1d   ≈
 demo-app         ▰▰✕  ✕ 4m                 —                         ▰▰▰⏸ ⏸ 12m  ◆
 ▸ j/k rows · h/l cells · ⏎ open env · p plan · . actions
```

**Run page.**

```
demo / demo-app / prod / run 0199c3…                                    ● live
┌──────────────────────────────────────────────────────────────────────────────────────────────┐
│ ⏸ AWAITING DECISION   tracked · a1b2c3d "add NAT" · triggered by bob@dev (manual) · 12m ago  │
│ Confirm needs: an approver with tier prod other than bob.  You: alice · approver · tiers ✓   │
│                                   [Discard]  [Cancel run]  [Confirm apply ▸ type "prod"]     │
├──────────┬───────────────────────────────────────────────────────────────────────────────────┤
│ ▰ Prep   │ ⚠ Plan computed on a1b2c3d — the branch is 2 commits ahead.  [Re-plan on head]    │
│   8s     │ [Plan +2 ~1 −1]  [Checks 2✓ 1⚠]  [Logs]  [Inputs ◆1]  [Discussion 2]             │
│ ▰ Plan   │ ┌───────────────────────────────────────────────────────────────────────────────┐ │
│   14s    │ │ −1 DESTROY  random_pet.legacy                         (always expanded)       │ │
│ ▰ Check  │ │ ±0 REPLACE                                                                    │ │
│   2⚠     │ │ +2 CREATE   ▸ local_file.a  ▸ local_file.b                                    │ │
│ ⏸ Apply  │ │ ~1 UPDATE   ▸ local_file.cfg                         1 tag-only hidden (show) │ │
│ ▱ Done   │ └───────────────────────────────────────────────────────────────────────────────┘ │
└──────────┴───────────────────────────────────────────────────────────────────────────────────┘
```

**Good at.** Lowest risk: DESIGN, SPECS, PLAN and the e2e already speak this language; each screen
is a page with a stable URL (incident links); straightforward responsive story (rail becomes a
horizontal strip, bar docks at the bottom). **Bad at.** It is the incumbent pattern of the market
plus a matrix: the 3 a.m. path is still "home → attention row → run page → scroll"; switching between
two pending decisions is a round trip; the matrix gets wide past ~6 environments.

### Direction B — "Triage console"

**Idea.** The product is a decision queue: everything that needs a human is a list, and the decision
happens in a pane next to it, keyboard-first (mail/issue-tracker model). The matrix is one view of
the list pane.

**Navigation.** Three panes: a narrow scope column (Needs you · Running · All environments ·
Graph · Queue · Audit · Settings), a list pane (items for that scope, `j/k`), a detail pane (the run
or env). ⌘K on top. On mobile the panes become a stack of screens (list → detail).

**Matrix ("All environments" scope).** Same matrix as A, rendered inside the list pane; selecting a
cell opens the env in the detail pane instead of navigating.

**Run page (detail pane).**

```
┌ scope ──┬ NEEDS YOU · 3 ──────────────────┬ demo-app / prod / run 0199c3…                    ┐
│ ● Needs │ ▶ ⏸ demo-app/prod   +2 ~1 −1    │ ⏸ AWAITING DECISION · bob · 12m · a1b2c3d ↑2      │
│   you 3 │     bob · 12m · ⚠ stale         │ Needs an approver (prod) other than bob — you ✓   │
│ ○ Runn. │   ⏸ demo-net/staging +0 ~3 −0   │ [d Discard] [x Cancel]          [c Confirm ▸]     │
│ ○ All   │   ✕ demo-net/dev   plan exit 1  │ ──────────────────────────────────────────────── │
│ ○ Graph │ ── WATCHING ──────────────────  │ −1 DESTROY random_pet.legacy                      │
│ ○ Queue │   ◐ demo-app/dev  applying 40s  │ +2 CREATE  …                                      │
│ ○ Audit │                                 │ rail: ▰ prep 8s ▰ plan 14s ▰ check 2⚠ ⏸ apply      │
└─────────┴─────────────────────────────────┴───────────────────────────────────────────────────┘
```

**Good at.** The fastest path from "paged" to "decided": open, read, `c`/`d`, `j` to the next one. It
fills gap 3 (approval clarity) by construction: the list says *needs you* vs *needs someone*. **Bad
at.** Three panes are cramped for logs and large plans (the detail pane needs a "pop out to full
page" anyway, so it ends up with A's run page too); the matrix is no longer the home, which goes
against the brief's goal 1; heavier routing/state work; the "Needs you" list depends on A4
(space-wide run query) to be cheap and on A2 to be exact.

### Direction C — "Living topology"

**Idea.** The matrix *is* the dependency graph: stacks are rows, environments are columns, and
the edges between cells are drawn on the matrix, labelled with the outputs they carry. A run opens as
a side sheet over the map; selecting a cell lights its downstream cascade.

**Navigation.** Top bar only (no side nav): Map · Queue · Audit · Settings, plus ⌘K. The map is home.

**Matrix.**

```
                 dev                                 prod
 demo-network    [▰▰▰▰ = 2h ↑3]──network_name──┐     [▰▰▰▰ ✓ 1d ≈]──network_name──┐
                                                │                                   │
 demo-app        [▰▰✕ ✕ 4m]◄────────────────────┘     [▰▰▰⏸ ⏸ 12m ◆]◄┄┄ mock ┄┄┄┄┄┄┘
 selected: demo-network/prod → cascade: demo-app/prod (on_output_change: runs if network_name changes)
```

**Run page.** A right-hand sheet (~60 % width) over the map, same content as A's run page; full page
on mobile.

**Good at.** Gap 1 and gap 5 together (multi-env overview *and* cascade visibility); the most
distinctive screen in the market. **Bad at.** Edges become spaghetti past a few dozen dependencies
(cross-stack, cross-env); laying out a matrix with routed edges is custom canvas work with real
accessibility cost (the list view must carry everything); the sheet-over-map run page is the wrong
container for a 3 a.m. destroy review; poor on mobile.

### Visual language — independent of the direction

These apply whichever direction you pick; each is a departure from DESIGN.md, justified against the
audience. They are proposals, see §9.

1. **Achromatic chrome; violet becomes brand-and-focus only.** Today violet fills the primary
   buttons, the active nav item and every link. Violet sits between running-blue (`#4C8DFF`) and
   mocked-magenta (`#EC6AB4`); under deuteranopia and tritanopia the three collapse towards each
   other, and at a glance a violet "New stack" button reads like a state. If every chromatic pixel in
   the chrome means a state, the matrix scans by colour alone in a second (with the label and icon
   still there for meaning). Primary buttons become high-contrast neutral (`text-primary` on
   `bg`), links are underlined text, violet stays on the logo, the focus ring and the selection.
2. **Amber narrowed to exactly one meaning.** Drift goes neutral with `≈` (as SPECS §19.5 already
   says); the plan's `~ update` goes neutral-bright with the glyph (an update is the *least* alarming
   action, it should not look like the decision colour); open threads and FALLBACK keep amber only if
   they block or demand the decision (FALLBACK does, per §15.5; open threads do not → neutral).
3. **Two outcomes of `finished`.** `✓ APPLIED +a ~c −d` (finished with `confirmed_at`) and
   `= NO CHANGES` (finished, zero summary) get different glyphs and labels; "planned" for proposed
   runs. Same green hue, distinct shape and text. Fixes gap 4 everywhere a run is shown.
4. **"Needs you" versus "needs someone".** Same amber, different label and weight: a solid amber
   badge when the current user can act, an outlined one when the run waits on someone else, with the
   reason. This is the reserved human signal made personal.
5. **Diff tokens separate from state tokens** (`--color-diff-create`, `-destroy`, `-update`,
   `-replace`) even where the hue is shared, so a future change to one does not silently change the
   other; destroy and replace always carry `−`/`±` and a text label.
6. **Contrast fixed, both themes**: every state colour ≥ 4.5:1 on the background it is actually drawn
   on (badge text is 11–12 px), non-text UI (input borders, focus ring) ≥ 3:1. Light theme reachable
   (follows the OS, in-memory override).
7. **Ship the fonts.** IBM Plex Sans + JetBrains Mono self-hosted (the identity of "data is mono"
   currently relies on whatever monospace the OS has).
8. **Type scale kept dense** (13 px UI, 12 px data, 11 px eyebrow only), titles reduced to 16/20 —
   page titles at 20 px currently eat the first row of every page for a word that the breadcrumb
   already says.

---

## 8. Recommendation

**Direction A as the skeleton, with one idea taken from B and one from C**, plus the visual-language
changes listed above.

Why A: the brief makes the matrix the strongest screen and the home, which rules B out as the
skeleton; C puts it first too, but its signature (edges routed across a table) is custom canvas work
that degrades with every added dependency and pushes the accessible equivalent into a second-class
list, and its sheet-over-map run page is the wrong container for a production destroy review. A
gives every entity a page with a stable URL, which is what an incident channel needs, and it is the
model SPECS, PLAN and the e2e scenario already use, so the migration risk sits in the visuals, not in
the information architecture.

What A borrows:

- **From B, the personal decision signal and the triage loop.** The attention zone says *needs you*
  (solid amber, you can act) versus *waiting on someone* (outlined, with who and why), and the run
  page has "next decision" (`]` / `[`) so a reviewer walks through pending decisions without going
  back home. This is gap 3 (approval clarity) without B's three-pane layout.
- **From C, cascade visibility on demand.** Not drawn permanently: on the run page, the decision bar
  lists the downstream environments the apply can trigger (BFS over `GET /graph` edges: `always` →
  "will plan", `on_output_change` → "plans if <outputs> change", `never` → omitted); on the matrix,
  selecting a cell outlines its downstream cells and names the outputs that flow. This is gap 5 with
  today's data.

How it answers the brief's five gaps: (1) matrix home with attention zone, lag, drift and mocked
signals per cell; (2) plan review with destroys and replaces first, tag-only changes collapsed but
counted, search; (3) decision bar that names the rule, the triggerer, the current user's standing and
what is still missing; (4) `✓ APPLIED` versus `= NO CHANGES`; (5) cascade preview before Confirm.

Consequences to plan for in stages 3–4: new routes (the old `/stacks/:id` and `/runs/:id` keep
working as redirects so shared links do not break), the Playwright happy path gains the matrix and
the run page, and an env page appears (today an environment has no page and no run history: only its
latest run is reachable).

---

## 9. Decisions I need from you

Stage 2 (rewriting DESIGN.md) starts from your answers to these.

1. **Direction.** A with the two borrowings (recommended), B, or C?
2. **Achromatic chrome.** Violet restricted to the logo, focus ring and selection; primary buttons
   and links neutral. Yes, or keep violet CTAs and active nav as today?
3. **Amber narrowed.** Drift neutral `≈` (aligns with SPECS §19.5), plan `~ update` neutral with its
   glyph, open-thread counter neutral; amber kept for awaiting-decision, warn checks at plan stages,
   FALLBACK. Yes / partly?
4. **URLs.** Put the space in the path (`/s/demo/stacks/demo-app/envs/prod`) and use names rather
   than UUIDs where they are unique in their parent (stack names are unique per space, env names per
   stack)? Runs stay `/runs/:id`. Old URLs redirect.
5. **Plan diff stopgap.** Parse the `tofu show` text from the logs until A1 exists, with a raw
   fallback that never hides a line. Acceptable, or do you prefer to do A1 in the API first?
6. **Dependencies for stage 3.** Already decided in DESIGN §8 but not installed: Radix primitives via
   shadcn (dialog, popover, dropdown-menu, tooltip, tabs), `cmdk` (⌘K), `react-virtuoso`, `anser`,
   self-hosted fonts (`@fontsource/ibm-plex-sans`, `@fontsource/jetbrains-mono`); plus the lint
   tooling the definition of done needs (`eslint`, `typescript-eslint`, `eslint-plugin-react-hooks`,
   `prettier`). OK to add all of these?
7. **API follow-ups.** Should I open issues for A1–A10 (§6) now, or keep them in this document until
   you have triaged them?
