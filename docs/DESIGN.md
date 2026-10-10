# DESIGN.md — Front-end design decisions

> Companion to PLAN.md and SPECS.md. This document locks in the design decisions for the interface: visual direction, tokens, navigation, patterns for the key screens. Goal: that every screen built (by a human or by Claude Code) is consistent without re-litigating the choices.

## What changed in v2 (front-end redesign, 2026-10)

v2 follows the stage-1 audit (`docs/redesign/AUDIT.md`, direction A "Blueprint, finished", validated).
Section numbers are kept so existing references (`DESIGN §5.2`, `§7`, `§8`…) still point to the same
subject. Changes versus v1:

| § | Change | Why (audit ref.) |
|---|---|---|
| 2.1 | **Achromatic chrome.** Brand violet is restricted to the logo, the focus ring and the selection mark; primary buttons and links are neutral. | Violet sat between running-blue and mocked-magenta and was the loudest colour on screen (C4). |
| 2.2 | Rail segments redefined (`Prepare · Plan · Check · Decision · Apply`) + an **outcome** cap; a failed run marks the failing segment; durations from log timestamps; mini variant spec'd. | The rail spun on finished runs and greyed out failed ones (C2). |
| 2.3 | **New: the environment summary rule** — an env is summarised by its active run, else its last *tracked* outcome, with drift/stale/lock/mock as modifiers. Never "the latest run". | The overview could show CANCELED while a prod decision was pending (C1). |
| 3.1 | Palette re-tuned for **AA on every background, both themes** (contrast table included); new `sunken`, `border-control`, `focus`, tint tokens. | 5 state colours failed AA in light, `queued` in both (C4). |
| 3.2 | **Two outcomes for `finished`** (APPLIED / NO CHANGES, + PLANNED, DESTROYED, EXECUTED); **"needs you" vs "waiting on someone"**; amber narrowed to the decision (drift, plan `~`, open threads become neutral). | Gap 4 and gap 3 of the brief; amber had five meanings (C4). |
| 3.3 | Type scale reduced (page titles 16 px); fonts **actually shipped** (self-hosted). | Fonts were never installed (C8). |
| 3.5–3.6 | **New:** diff tokens separate from state tokens; the full meaning → token → glyph → label map. | One hue, one meaning. |
| 4 | Navigation: Overview replaces Stacks as home; **space and names in the URL**; env page; Queue in the nav; responsive model (drawer < 1024 px, bottom bar < 768 px); keyboard map; ⌘K command list. | C5, C6. |
| 5.1 | Overview matrix fully specified: `EnvCell`, attention zone with *needs you* first, cascade preview on selection, URL filters, `j/k/h/l`. | Matrix was not built (C1, C6). |
| 5.2 | Run page rebuilt around the **decision bar**: reasons for every disabled action, four-eyes status, cascade preview, stale and mocked banners, Plan/Checks/Logs/Inputs/Discussion tabs, next-decision navigation. | C2. |
| 5.2 | **PlanDiff** parsed from the `tofu show` log section until the API serves the structured plan (AUDIT A1); destroys and replaces first and never collapsible; tag-only updates collapsed but counted; search. | Plan was three numbers (C2). |
| 5.3 | LogViewer spec'd for incremental fetch (`after_seq`), ordering by time, sections, first error. | C3. |
| 5.4 | Graph: edge labels = output names; magenta only when a mock was **consumed**; node drawer; run-group view deferred (AUDIT A5). | C1, C4. |
| 5.7 | Friction added to safety switches (four-eyes off, `protected` off, role/tier grants, force-unlock); no icon-only destructive control. | C7. |
| 5.8 | **New environment page** with run history. | Older runs were unreachable (C6). |
| 5.10 | New: variable sets, settings, login, notifications. | — |
| 8 | Folder structure, primitives, identity component contracts, lint/format tooling, story requirements. | C8. |

What did **not** change: dark-first, density, flat and ruled, "data is mono", the phase rail as
signature, amber = needs a human, magenta = mocked modifier, typed-name friction, destroys never
hidden, no browser storage, tokens only, WS invalidates queries (logs streamed).

Items that need API work are listed in `docs/redesign/AUDIT.md` §6 (A1–A10); every screen below is
specified with a version that works on today's API, marked **[today]**, and the exact version once
the API exists, marked **[with Ax]**.

---

## 1. Subject, audience, intent

- **Subject**: a control room for infrastructure. You come here to quickly answer three questions: *what is running?*, *what is waiting on a human decision — and is that human me?*, *who did what?*
- **Audience**: cloud/DevOps/SRE engineers. Expert, daily users, on the keyboard, often on a large screen, sometimes on call on whatever screen is at hand. Zero need for marketing seduction in the app.
- **Emotional intent**: **operational confidence**. The interface of a tool that applies destructive changes to production must be calm, dense, legible, unambiguous. No gamification, no decoration, no gratuitous animation.
- **The 3 a.m. test.** Every screen is judged by one scenario: an on-call engineer, woken up, on a laptop or a phone, must be able to tell in five seconds whether something waits on *them*, what it will change, and what happens if they click. Any element that can make them believe something false (a stale summary, an enabled button that will be refused, a hidden destroy) is a defect, not a polish item.
- **Anti-references**: the "generic SaaS dashboard" look (floating rounded cards, gradients, 3D illustrations) and the "hacker terminal" look (pure black + acid green). Both are defaults, not choices.

---

## 2. Visual direction

### 2.1 Concept: "operational blueprint", achromatic chrome

The aesthetic draws on **technical drawings and engineering schematics**: deep slate-blue backgrounds, fine strokes, monospace labels, hierarchy carried by typography and rules rather than by shadowed boxes.

- **Dark-first**: the dark theme is the reference theme (logs, on-call duty, the habits of the trade). The light theme is a full peer: same tokens, same contrast requirements, reachable from the UI (§8).
- **Deliberately high density**: compact tables, tight line-height on data, generous margins only around decision areas. Density is a service rendered to an expert, not a defect.
- **Flat and delimited by rules**: separations via 1px borders and subtle background variations. No drop shadows, no glassmorphism. Low radius (4 px) — the tool is angular like a schematic.
- **Achromatic chrome (v2).** Navigation, buttons, links, headings, tables and forms are drawn in the neutral scale only. **Every chromatic pixel on a screen means a state** (§3.2) or a diff (§3.5). The eye can then scan the matrix by colour in one second, and the label and glyph next to each colour carry the meaning for those who cannot see it.
  - Primary button: `text-primary` fill with `on-fill` text (the highest-contrast element of the chrome, without a hue).
  - Links: `text-primary`, underlined (1 px, offset 2 px); visited links are not distinguished.
  - Active nav item: `bg-raised` + `text-primary` + a 2 px `focus`-coloured bar on the left (the selection mark).
  - **Brand violet** survives in exactly three places: the logo's "D", the **focus ring**, and the **selection mark** (active nav, selected matrix cell, selected row). It never fills a button and never carries a state.

### 2.2 Signature element: the **phase rail**

THE memorable thing about the interface: a **vertical rail** on every run page that materializes the state machine (SPECS §4), and its **mini** horizontal variant everywhere a run is listed.

**Segments** (in order):

| Segment | Covers run states | Shown when |
|---|---|---|
| `Prepare` | `queued` (as pending), `preparing` | always |
| `Plan` | `planning` | always |
| `Check` | `checking` | iff the plan ran `after_plan` hooks (SPECS §4.2), or the run is in `checking` now |
| `Decision` | `unconfirmed`, `confirmed` | always for `tracked`/`destroy`; shows **auto** when confirmed by the system (autodeploy), **not needed** when the plan was empty, absent for `proposed` runs |
| `Apply` | `applying` | `tracked`/`destroy`; **not needed** when the plan was empty |

`command` runs use `Prepare · Run`. After the last segment, the rail ends with the **outcome cap**: the
terminal outcome label of §3.2 (`APPLIED +3 ~1 −0`, `NO CHANGES`, `FAILED at Check`, `DISCARDED`,
`CANCELED`, `PLANNED`, `DESTROYED −3`, `EXECUTED`), or nothing while the run is live.

**Segment status** (each status = colour + glyph + text, never colour alone):

| Status | Glyph | Colour token | Meaning |
|---|---|---|---|
| pending | `○` | `state-queued` at full opacity, dashed stroke | not reached yet |
| active | `◐` | `state-running` + pulse | the run is in this phase now |
| waiting | `⏸` | `state-decision` | the run waits on a human here (only the Decision segment) |
| done | `●` (filled) | `state-finished` | completed |
| failed | `✕` | `state-failed` | the run failed **in this phase** |
| not needed | `–` | `text-secondary` | skipped by design (empty plan, no after_plan hooks shown as absent) |
| not run | `○` | `text-secondary`, struck | the run ended (discarded, canceled, failed) before this phase |

**Failed segment**: `Apply` if `confirmed_at` is set; otherwise the segment of the stage of the first
`fail` check (`before_init`/`after_init` → Prepare, `before_plan` → Plan, `after_plan` → Check);
otherwise `Plan` (a tofu failure before a plan exists). A failure during Apply is labelled
**"FAILED during apply — infrastructure may be partially changed"**: that sentence is the most
important thing on the page and is never truncated.

**Metadata** under each vertical segment (mono, `text-secondary`): duration computed from the first
and last log line timestamps of that phase **[today]**, exact from run events **[with A7]**; Plan: the
summary `+a ~c −d`; Check: `2 ok · 1 warn`; Decision: `alice · 12:04` or `auto` or `waiting 12m`.

**Interaction**: each vertical segment is a button that opens the Logs tab scrolled to that phase's
first section (`?tab=logs&section=planning`). Keyboard: segments are in the tab order after the
action bar.

**Mini variant**: 5 cells of 10 × 4 px, 2 px gaps, same statuses (pending = 1 px outline, done =
fill, failed = fill + `✕` cap glyph after the rail, waiting = fill amber), followed by the outcome
glyph. `role="img"` with an `aria-label` that reads the outcome: "Unconfirmed, needs your decision, plan +2 ~1 −0".

**Motion**: the active segment pulses (opacity 1 → 0.45, 1.6 s) — the only ambient animation in the
app; `prefers-reduced-motion` replaces it with a static 0.7 opacity.

### 2.3 The truth of an environment (new in v2)

Wherever an environment is summarised — the overview matrix (`EnvCell`), the stack page, the graph
node, the ⌘K results — it is summarised by **one rule**, implemented once (`lib/envSummary.ts`) and
unit-tested:

1. **Primary run** = the environment's **active run** if any (non-terminal state; unique per env,
   SPECS §3.5). If that run is `proposed` it is shown with a `PR` marker, since it occupies the env.
2. Otherwise, the **last terminal `tracked` or `destroy` run whose outcome is not DISCARDED or
   CANCELED**. `proposed` runs (PR plans), drift-check runs (`triggered_by=schedule`) and `command`
   runs **never** stand for the env.
3. **Modifiers**, independent of the primary run, each with its own glyph (§3.6): drift
   (`drift_status`), stale (`stale`, `commits_ahead`), locked (`locked`), mocked (primary run
   `used_mocks`), fallback (primary run `used_secret_fallback`).
4. **Deployed commit** = `commit_sha` of the last `finished` tracked run (applied or no changes),
   shown on hover and on the env page.

Consequence: an env whose last run was discarded still shows its last real outcome; an env with an
unconfirmed plan shows UNCONFIRMED even if newer runs were queued and canceled.

---

## 3. Design tokens

### 3.1 Palette

Tokens are CSS custom properties on `:root` / `[data-theme]` (§8). Contrast ratios are WCAG 2.x,
computed for each foreground on each background it is drawn on. Requirement: **≥ 4.5:1 for any
text** (badges are 11–12 px), **≥ 3:1 for non-text UI** (control borders, focus ring, rail strokes).
`border` is a decorative rule and is exempt; controls use `border-control`.

**Surfaces**

| Token | Dark | Light | Usage |
|---|---|---|---|
| `bg-base` | `#0C1118` | `#F6F8FA` | app background |
| `bg-surface` | `#141B24` | `#FFFFFF` | panels, tables, the matrix |
| `bg-raised` | `#1D2733` | `#EEF2F6` | hover, sticky headers, popovers, the action bar |
| `bg-sunken` | `#090D13` | `#EAEEF2` | log viewer, code blocks, plan attribute diffs |
| `border` | `#2A3542` | `#D3DAE2` | 1 px structural rules (decorative) |
| `border-control` | `#6B7A8B` | `#7D8A97` | input, select, checkbox and secondary-button borders |
| `overlay` | `rgba(0,0,0,.55)` | `rgba(27,39,51,.45)` | modal scrim only |

**Text and structure**

| Token | Dark | Light | Usage |
|---|---|---|---|
| `text-primary` | `#E8EEF4` | `#1B2733` | body, data, primary button fill |
| `text-secondary` | `#9AA6B2` | `#55626F` | labels, meta, eyebrows |
| `focus` | `#A28BFF` | `#6D3FD4` | focus ring, selection mark (brand violet) |
| `brand` | `#8B6CF0` | `#6D3FD4` | the logo only |
| `on-fill` | `#0C1118` | `#FFFFFF` | text and glyphs on a filled button or a solid badge (primary, decision, danger, mock) |

**States and modifiers** (semantics in §3.2)

| Token | Dark | Light |
|---|---|---|
| `state-queued` (neutral) | `#9AA6B2` | `#5C6875` |
| `state-running` | `#6EA8FF` | `#0B5CD5` |
| `state-decision` (= `state-unconfirmed`) | `#F0B445` | `#8A5300` |
| `state-finished` | `#4CC35E` | `#167032` |
| `state-failed` | `#FF7B72` | `#C4212C` |
| `mock` | `#F07CC0` | `#A3287F` |

**Tints** — a row or banner background that carries a state, always with the state's glyph and label:

| Token | Dark | Light | Usage |
|---|---|---|---|
| `decision-tint` | `#2A2213` | `#FFF3D6` | "needs you" attention rows, the action bar when the user can act |
| `failed-tint` | `#2A1517` | `#FFEBE9` | failed banner, failed-during-apply banner |
| `mock-tint` | `#2A1626` | `#FCEBF6` | mocked banner |

**Measured contrast** (min over the backgrounds listed):

| Foreground | Dark: base / surface / raised / sunken | Light: base / surface / raised / sunken |
|---|---|---|
| `text-primary` | 16.2 / 14.8 / 12.9 / 16.7 | 14.3 / 15.2 / 13.5 / 13.0 |
| `text-secondary` | 7.6 / 7.0 / 6.1 / 7.9 | 5.9 / 6.2 / 5.6 / 5.4 |
| `state-queued` | 7.6 / 7.0 / 6.1 / 7.9 | 5.3 / 5.7 / 5.1 / 4.9 |
| `state-running` | 7.9 / 7.2 / 6.3 / 8.1 | 5.6 / 6.0 / 5.3 / 5.1 |
| `state-decision` | 10.2 / 9.3 / 8.1 / 10.5 | 6.0 / 6.3 / 5.6 / 5.4 |
| `state-finished` | 8.4 / 7.7 / 6.7 / 8.6 | 5.8 / 6.2 / 5.5 / 5.3 |
| `state-failed` | 7.5 / 6.9 / 6.0 / 7.7 | 5.5 / 5.8 / 5.2 / 5.0 |
| `mock` | 7.5 / 6.9 / 6.0 / 7.7 | 6.2 / 6.6 / 5.9 / 5.7 |
| `focus` (non-text, ≥ 3) | 6.9 / 6.3 / 5.5 / 7.1 | 6.0 / 6.3 / 5.6 / 5.4 |
| `border-control` (non-text, ≥ 3) | 4.3 / 4.0 / 3.4 / 4.4 | 3.3 / 3.5 / 3.1 / 3.0 |

On tints, every state colour and both text colours stay ≥ 5.0:1 (dark ≥ 6.2, light ≥ 5.1). `on-fill` on fills (dark / light): on `state-decision` 10.2 / 6.3; on `text-primary` 16.2 / 15.2;
on `state-failed` 7.5 / 5.8; on `mock` 7.5 / 6.6.

Trade-off, stated: AA forces the light amber down to `#8A5300`, which reads as ochre rather than
signal amber. It is still the only warm hue of the light theme, and the decision is always carried by
the `⏸` glyph and the "needs you" label as well.

**Disabled controls** do not use opacity. They use `text-secondary` on `bg-raised` with a dashed
`border-control`, keep ≥ 4.5:1, and always carry their reason (§5.2).

### 3.2 State language (identical semantics in both themes)

**Run states** — badge = glyph + uppercase mono label + colour:

| State(s) | Badge label | Glyph | Token |
|---|---|---|---|
| `queued` | QUEUED | `○` | `state-queued` |
| `preparing` `planning` `checking` `confirmed` `applying` | PREPARING / PLANNING / CHECKING / APPLYING (`confirmed` shows APPLYING · STARTING) | `◐` (pulse) | `state-running` |
| `unconfirmed` | UNCONFIRMED (the SPECS state name, kept for vocabulary consistency; the personal form below says what it means for the viewer) | `⏸` | `state-decision` |
| `failed` | FAILED (+ `at <segment>`) | `✕` | `state-failed` |
| `discarded` | DISCARDED | `⊘` | `state-queued`, label struck through |
| `canceled` | CANCELED | `⊘` | `state-queued`, label struck through |

**Outcomes of `finished`** (v2 — "succeeded" is never ambiguous):

| Outcome | Rule (today's data) | Label | Glyph | Token |
|---|---|---|---|---|
| Applied | `tracked`, `confirmed_at` set | APPLIED `+a ~c −d` | `✓` | `state-finished` |
| No changes | `tracked`, no `confirmed_at` (empty plan) | NO CHANGES | `=` | `state-finished` |
| Destroyed | `destroy`, `confirmed_at` set | DESTROYED `−d` | `✓` | `state-finished` |
| Planned | `proposed` | PLANNED `+a ~c −d` | `◇` | `state-finished` |
| Executed | `command` | EXECUTED `<command>` | `✓` | `state-finished` |

Applied and No changes share the green (both are successes) but never the glyph, the label or the
counts. On the matrix, *No changes* is visually quieter: its label is `text-secondary`, only the `=`
glyph is green.

**The human signal, made personal** — amber means "a human decision is pending", and nothing else.
Two forms:

| Form | When | Rendering |
|---|---|---|
| **Needs you** | `unconfirmed` and the current user may **confirm** it (§5.2 rules; being able to discard is not enough) | **solid** amber badge (`on-fill` text on `state-decision`), `decision-tint` row background in lists |
| **Waiting on someone** | `unconfirmed` and the current user cannot confirm | **outlined** amber badge, with the reason in words ("needs an approver with tier prod other than you") |

**Modifiers** overlay any state, are never colour-only, and stack in a fixed order after the badge:

| Modifier | Label / glyph | Token | Note |
|---|---|---|---|
| Mocked | `◆ MOCKED` | `mock` | not real; blocks apply unless `allow_mock_apply` (SPECS §9.3) |
| Fallback | `◈ FALLBACK` | `state-decision` | a secret came from a fallback; blocks apply unless `allow_fallback_apply` (SPECS §15.5) — it demands a decision, hence amber |
| Stale | `↑3` (or `↑` until AUDIT A6) | `text-secondary` | branch ahead of the deployed commit; attenuated (`text-secondary`, dotted underline) when `affects_project_root=false` |
| Drift | `≈ DRIFT` / `≈? DRIFT CHECK FAILED` | `text-primary` | neutral per SPECS §19.5 — a fact to investigate, not a decision |
| Locked | `⚿ LOCKED` | `text-primary` | |
| PR | `PR` | `text-secondary` | the env's active run is a proposed run |

Absolute rule: these colours serve **only** state semantics. Never decorative blue, never green on a
button unrelated to a success, never amber for telemetry or log severity.

### 3.3 Typography

| Role | Font | Size / line-height | Usage |
|---|---|---|---|
| Display | IBM Plex Sans SemiBold, tracking −1 % | 20 / 28 | login, empty states only |
| Page title | IBM Plex Sans SemiBold | 16 / 24 | one per page |
| Section | IBM Plex Sans SemiBold | 14 / 20 | panel and tab-section headings |
| UI body | IBM Plex Sans Regular | 13 / 20 | navigation, forms, prose |
| Data | JetBrains Mono Regular, `tabular-nums` | 12 / 18 | IDs, SHAs, addresses, values, durations, tables, logs (logs 12 / 18) |
| Eyebrow | JetBrains Mono Medium, uppercase, tracking +4 % | 11 / 16 | column headers, badge labels, field labels |
| Big number | JetBrains Mono Medium, `tabular-nums` | 20 / 28 | plan summary on the run page and the confirm popover |

**"Data is mono" rule**: everything that comes from the system — a `commit abc1234`, a `vpc-0f3a…`, a
`+3 ~1 −0`, a duration — is in mono; everything the interface says is in Sans. Weights: 400, 500
(mono labels), 600. Fonts are **self-hosted** (`@fontsource`), Latin subset, weights Sans 400/600 and
Mono 400/500 preloaded, `font-display: swap`, fallback metrics overrides to avoid layout shift.

### 3.4 Spacing, radius, elevation

- 4 px grid. Dense table cell padding 6 × 10 px (row height 32 px); comfortable rows (attention zone, run history) 8 × 12 px (36 px); forms 12 px gaps.
- Page gutters: 24 px ≥ 1024 px, 16 px below.
- Radius: 4 px everywhere, 2 px on badges. No pills.
- Elevation: no shadows. Overlays (popover, dropdown, dialog, sheet) are `bg-raised` with a 1 px `border-control` border; modal dialogs add the `overlay` scrim.
- Iconography: Lucide, 16 px, stroke 1.5, always accompanied by a visible label when the action is destructive or state-changing. Pure icon buttons are allowed only for non-destructive tools (copy, refresh, collapse) and always have an `aria-label` and a tooltip.

### 3.5 Diff tokens (new in v2)

Plan diffs keep the convention every Terraform user knows, through **dedicated tokens** so a change to
a state colour never silently changes the diff (they alias the state values today):

| Token | Value | Glyph | Label in PlanDiff |
|---|---|---|---|
| `diff-create` | = `state-finished` | `+` | CREATE |
| `diff-destroy` | = `state-failed` | `−` | DESTROY |
| `diff-replace` | = `state-failed` | `±` | REPLACE |
| `diff-update` | = `text-primary` | `~` | UPDATE |
| `diff-read` / no-op | = `text-secondary` | `<=` / `·` | READ / NO-OP |

The update is **neutral** (v1 used amber): an in-place update is the least alarming action, and amber
is reserved to the decision. Zero counts are rendered in `text-secondary` without colour
(`+4 ~0 −0` colours only the `+4`).

### 3.6 Meaning map — one hue, one meaning

| Meaning | Colour | Glyph | Text always present |
|---|---|---|---|
| Something is executing | running blue | `◐` | PLANNING / APPLYING… |
| A human decision is pending | amber | `⏸` | UNCONFIRMED + NEEDS YOU / WAITING ON … |
| A value is not the real secret (fallback) | amber | `◈` | FALLBACK |
| Success (applied, no changes, planned…) | green | `✓ = ◇` | outcome label |
| Failure | red | `✕` | FAILED at … |
| Not real (mock) | magenta | `◆` | MOCKED / MOCK |
| Neutral, waiting for a machine | grey | `○` | QUEUED |
| Ended without effect | grey, struck | `⊘` | DISCARDED / CANCELED |
| Focus, selection, brand | violet | ring / bar | — |
| Drift, stale, lock, PR | no hue | `≈ ↑ ⚿ PR` | DRIFT / ↑3 / LOCKED / PR |
| Diff create / destroy / replace / update | green / red / red / neutral | `+ − ± ~` | CREATE / DESTROY / REPLACE / UPDATE |
| Telemetry (HTTP status, log level, worker online) | neutral; red **only** for errors | — | the value itself |

---

## 4. Navigation architecture

```
┌──────────────┬───────────────────────────────────────────────────────────┐
│ StackD       │ demo / demo-network / prod / run 01a1276c   ● live  ⌘K  ☾ │ ← top bar
│ ─────────    ├───────────────────────────────────────────────────────────┤
│ ▣ Overview 3 │                                                           │
│ ⎇ Graph      │                       content                             │
│ ≡ Queue   1  │        (max width 1440 px, tables and matrix full width)  │
│ ▥ Workers    │                                                           │
│ ▤ Audit      │                                                           │
│ ⚙ Var. sets  │                                                           │
│ ⚙ Settings   │                                                           │
│ ─────────    │                                                           │
│ space: demo ▾│                                                           │
│ alice · appr.│                                                           │
└──────────────┴───────────────────────────────────────────────────────────┘
```

- **Side nav (208 px, labels always visible — v1 decision kept)**: Overview (count badge = decisions that need *you*), Graph, Queue (count badge = runs waiting > 2 min), Workers, Audit (admin only, §5.6), Variable sets, Settings. At the bottom: the **space switcher** (a Radix dropdown listing spaces with their stack counts) and the user block (email, role, allowed tiers, theme toggle, sign out). Active item: selection mark (§2.1).
- **Top bar**: entity breadcrumb, connection indicator (`● live` / `◌ reconnecting…`), ⌘K button, notification bell.
- **Breadcrumb**: built from entities, never from path segments: `space / stack / env / run 01a1276c`. Each segment is a link; the run segment copies the run URL on click.
- **Responsive**: ≥ 1024 px as above. 768–1023 px: the side nav becomes a drawer opened from a menu button in the top bar. < 768 px: a **bottom bar** with three destinations (Overview, Queue, Menu — notifications stay in the top bar at every width) and the run page's action bar docked above it.

### 4.1 URLs (v2)

| Route | Page |
|---|---|
| `/` | redirect to `/s/<space>` — the first space (by name) **that has stacks**, else the first space |
| `/s/:space` | Overview (matrix + attention zone) |
| `/s/:space/stacks/:stack` | Stack page |
| `/s/:space/stacks/:stack/envs/:env` | Environment page (§5.8) |
| `/runs/:runId` | Run page (space resolved from the run) |
| `/s/:space/graph` | Graph |
| `/queue`, `/workers`, `/audit`, `/settings`, `/variable-sets` | instance or space-scoped pages (variable sets: `/s/:space/variable-sets`) |

Names are used instead of UUIDs where they are unique in their parent (space names; stack per space;
env per stack — SPECS constraints). A renamed entity breaks old name-based links, so **v1 UUID routes
stay as redirects** (`/stacks/:uuid` → `/s/<space>/stacks/<name>`), and the run URL stays UUID-based
(it is the incident link).

**Everything that determines what is on screen is in the URL**: tab (`?tab=plan`), filters (`?q=app&state=attention&tier=prod`), log position (`?tab=logs&line=planning.87`), plan search (`?plan_q=nat`), graph filter and view (`?env=prod&view=list`). The active space is the URL's, never in-memory state. (No browser storage, CLAUDE §4.8.)

### 4.2 ⌘K command palette

Opened with ⌘K / Ctrl-K from anywhere (`cmdk` on a Radix dialog). Sections, in order:
1. **Needs you** — pending decisions the user can act on (jump to the run).
2. **Go to** — stacks, environments (`demo-network/prod`), recent runs by short ID, pages.
3. **Actions on the current page** — Plan this env, Plan with downstream, Confirm / Discard / Cancel (opens the same confirmation as the button, never bypasses it), Re-plan, Copy run link, Copy commit SHA, Toggle theme.
4. **Spaces** — switch space.

Results show the env summary glyphs (§2.3). The palette never performs a state-changing action
without the same friction as the button it mirrors.

### 4.3 Keyboard map

| Key | Where | Action |
|---|---|---|
| `⌘K` / `Ctrl-K` | everywhere | palette |
| `g o` / `g q` / `g g` / `g a` | everywhere | go to Overview / Queue / Graph / Audit |
| `?` | everywhere | keyboard help |
| `/` | lists, matrix, plan, logs | focus the filter / search |
| `j` `k` | lists, matrix rows, attention zone | next / previous row |
| `h` `l` | matrix | previous / next cell in the row |
| `Enter` | list, matrix | open |
| `]` `[` | run page | next / previous run that needs you |
| `1`…`5` | run page | tabs Plan, Checks, Logs, Inputs, Discussion |
| `c` / `d` / `x` | run page | Confirm (opens its friction), Discard, Cancel — only when enabled |
| `f`, `t`, `w`, `e` | log viewer | full screen, timestamps, wrap, jump to first error |
| `Esc` | overlays | close, focus returns to the trigger |

Single-letter shortcuts are inactive while focus is in a text field, and can be disabled from the
user menu (WCAG 2.1.4); the setting lives in memory.

---

## 5. Key screens — decisions

### 5.1 `/s/:space` — the overview (home)

The answer to "what is running, what waits on me, what is wrong" in one screen.

```
Overview · demo                                       filter [ app____ ]  state [needs attention ▾]  tier [all ▾]
┌ ATTENTION · 5 ─────────────────────────────────────────────────────────────────────────────────────────────┐
│ NEEDS YOU                                                                                                   │
│ ▌⏸ demo-network / staging  UNCONFIRMED · NEEDS YOU  +1 ~1 −1   bob · 12m   ↑ stale plan                  ⏎  │
│ WAITING ON SOMEONE                                                                                          │
│  ⏸ demo-network / prod     UNCONFIRMED  +4 ~0 −0   bob · 20m   needs an approver (prod) ≠ bob               │
│ FAILED · DRIFT · BLOCKED                                                                                    │
│  ✕ demo-dns / dev          FAILED at Check    policy: require owner tag             6m                      │
│  ◆ demo-app / prod         MOCKED · apply blocked (allow_mock_apply off)            20m                     │
│  ≈ demo-app / staging      DRIFT  checked 1h ago                                                            │
└─────────────────────────────────────────────────────────────────────────────────────────────────────────────┘
 STACK ▴          dev                          staging                        prod                four-eyes
 ──────────────── ──────────────────────────── ────────────────────────────── ───────────────────────────────
 demo-app         ▰▰▰▰ ✓ APPLIED +3   2h       ▰▰▰▰ ✓ APPLIED +3   1h  ≈       ▰▰⏸· ⏸ 20m  ◆
 demo-dns         ▰▰✕   ✕ FAILED at Check 6m   ▰▰⏸· ⏸ DESTROY −3  9m            —
 demo-network     ▰▰⏸· ⏸ 9m  ↑                 ▰▰⏸· ⏸ 12m  ↑                  ▰▰⏸· ⏸ 20m
 j/k rows · h/l cells · ⏎ open env · r open run · p plan · s show downstream
```

**Attention zone** (pinned above the matrix, never auto-collapsed while it holds a *needs you* item):

- Groups, in order: **Needs you** (unconfirmed, the user can confirm), **Waiting on someone** (unconfirmed, the user cannot confirm — with the missing condition in words), **Failed** (env's primary run failed), **Drift** (`drifted` or check error), **Blocked** (primary run mocked or fallback with apply not allowed), **Stale plans** (unconfirmed run whose commit ≠ env head). A row appears in the first matching group only.
- Row: summary glyph + `stack / env` + badge + plan summary + who/age + the reason or next step + Enter target (the run page, Plan tab for decisions, Logs tab at first error for failures, env page for drift).
- Empty zone: one line, "Nothing needs attention in demo." — not hidden, so its absence is never ambiguous.

**Matrix**:

- Rows = stacks of the space (sortable by name, by "worst state", by last activity). Columns = environment **names** present in the space, ordered by tier `position`, then env `position`, then name; a stack without that env shows `—`. A column header shows the env name, plus `four-eyes` when every env in the column is on a `requires_four_eyes` tier.
- Sticky header row and first column. Above 8 columns the matrix scrolls horizontally inside its own container, never the page.
- **`EnvCell`** (one per env, 200 px min): line 1 = mini rail + outcome/state glyph + short label (+ plan summary if relevant); line 2 = relative age + modifiers (`↑` `≈` `⚿` `◆` `◈` `PR`). Cell background stays `bg-surface`, except `decision-tint` when the cell *needs you*. Hover/focus shows a popover: branch, deployed commit, head commit, last run, triggerer, and the actions the user can take (Open, Plan, Open run).
- **Cascade preview on demand**: `s` (or "Show downstream" in the cell menu) outlines the selected cell's downstream cells (BFS over `GET /graph` edges) with the selection mark, and a strip under the matrix lists "demo-network/prod → demo-app/prod via `network_name` (on_output_change)". `Esc` clears.
- **Filters** in a top bar, reflected in the URL: text (stack or env name), state (`all`, `needs attention`, `needs you`, `running`, `failed`, `drift`, `stale`), tier. Filtering by state hides non-matching rows, not cells.
- **Keyboard**: the matrix is a `role="grid"` with roving tabindex (`h/j/k/l`, arrows, Enter); the attention zone is a list before it in the tab order.
- **Data** **[today]**: `stacks.list` → `stacks.environments` per stack → `runs.list` per env (the summary needs the active run and the last tracked outcome), one WebSocket subscribed to every `environment:<id>` channel invalidating `["env-runs", id]`, 30 s poll fallback. Skeleton cells while loading. **[with A4]** one request.
- **Empty states**: no stack → "No stack in demo yet. Connect a repository." + New stack (writer+); no stack in this space but in others → list them.
- **< 768 px**: the attention zone first, then one block per stack listing its envs as rows (`EnvCell` full width). No horizontal scroll.

### 5.2 `/runs/{id}` — the most important screen

```
┌ demo / demo-network / prod / run 01a1276c ─────────────────────────────────────────── ● live ┐
│ tracked · 2af0351 "widen subnet, drop NAT" · jdoe · branch main · triggered by bob (manual) · 20m ago │
├──────────────────────────────────────────────────────────────────────────────────────────────┤
│ ⏸ UNCONFIRMED · NEEDS YOU                                                     ← RunActionBar │
│ Four-eyes (tier prod): bob triggered this run — another approver with tier prod must confirm.│
│ You (alice · approver · dev staging prod) can confirm.                                       │
│ This apply can trigger: demo-app/prod (when network_name changes)                            │
│                        [Cancel run]  [Discard]   [Confirm apply ▸]   ◁ ] next decision ▷    │
├──────────────────────────────────────────────────────────────────────────────────────────────┤
│ ↑ Plan computed on 2af0351. The branch head is now 125304e (ahead).    [Re-plan on head]     │
├────────────┬─────────────────────────────────────────────────────────────────────────────────┤
│ ● Prepare  │ [Plan +4 ~0 −1] [Checks 2 ok] [Logs] [Inputs] [Discussion 1]                     │
│   6s       │                                                                                  │
│ ● Plan     │ +4 ~0 −1            search [ nat_____ ]                                          │
│   14s      │ − DESTROY  1  ─────────────────────────────────────────── (always expanded)      │
│ ⏸ Decision │   − terraform_data.nat_gateway                                                   │
│   20m      │       − input = "nat-eu-west-1" -> null                                          │
│ ○ Apply    │ + CREATE   4  ▸ terraform_data.network  ▸ …subnet_private  ▸ …subnet_db  ▸ …     │
└────────────┴──────────────────────────────────────────────────────────────────────────────────┘
```

**Header** (identity, read-only): breadcrumb; run type; commit SHA (copy) + message + author;
branch; how it was triggered (`manual by bob`, `webhook`, `dependency from demo-network/prod`
**[today: upstream env]** / `[with A5: upstream run]`, `schedule (drift check)`); age (relative,
absolute ISO on hover); total duration; worker. A `PR` marker for proposed runs.

**`RunActionBar`** — sticky under the top bar on desktop, docked at the bottom on < 768 px:

1. **State line**: `StateBadge` (with outcome and modifiers), then one sentence that says what the
   run waits for or how it ended ("Applied 3 changes 2h ago, confirmed by alice", "No changes —
   nothing was applied", "Failed during apply — infrastructure may be partially changed").
2. **Four-eyes line**, whenever four-eyes applies to the env (tier `requires_four_eyes` — fail-closed
   when the tier is missing from the catalog —, `protected`, or `require_second_pair_of_eyes`):
   who triggered, and the consequence. When the run is finished: "Triggered by bob · confirmed by
   alice 12:04".
3. **Your standing**: "You (alice · approver · dev staging prod) can confirm" or the first blocking
   reason. **[today]** computed from `/auth/me`, labelled "based on your instance permissions" when
   the space has per-space memberships the client cannot read; **[with A2]** exact. "Who may still
   confirm" is stated as a rule (**[today]**) — names come with A3.
4. **Cascade preview**: downstream envs from `GET /graph` (BFS): `always` → "will plan", 
   `on_output_change` → "plans if <outputs> change", `never` → omitted. Mock edges marked.
5. **Actions**, right-aligned, in this order: Cancel run · Discard · **Confirm** (amber, the only
   amber button of the app) · next-decision arrows.

**Action availability and disabled reasons** — an action that does not apply to the run's state is
**not rendered**; an action that applies but is refused is rendered **disabled with its reason as
visible text** under the bar (and as `aria-describedby`), never only in a tooltip:

| Action | Rendered when state is | Disabled when (reason text) |
|---|---|---|
| Confirm | `unconfirmed` | role not approver/admin ("approver role required"); `env.tier ∉ allowed_tiers` ("tier prod is not in your allowed tiers: dev, staging"); four-eyes and `trigger_user_id = you` with `triggered_by=manual` ("you triggered this run; four-eyes requires someone else"); `type=destroy` and not `can_destroy` ("destroy permission required"); `used_mocks` and not `allow_mock_apply` ("mocked inputs — apply disabled on this environment"); `used_secret_fallback` and not `allow_fallback_apply` ("a secret came from a fallback — apply disabled") |
| Discard | `unconfirmed` | role reader ("writer role required") — SPECS §2.3 |
| Cancel | `queued`, `unconfirmed` | role reader; on executing states it is not rendered, and the bar says "Cancelling a running job is not supported yet" (SPECS §4.1) |
| Re-plan on head | `unconfirmed` with a stale plan, or any terminal state ("Plan again") | role reader |

Whatever the client computed, when the API refuses an action its `detail` is shown verbatim in a
`failed-tint` banner under the bar.

**Friction proportional to risk** (unchanged in substance): an authorized apply on an ordinary env =
one click (the button shows a spinner, double submission impossible). On a **four-eyes env** (as
above) or **any `destroy`**, Confirm opens a **popover anchored to the button** (a bottom sheet on
mobile) containing: the plan summary in big mono; the **list of destroyed and replaced addresses**
(from PlanDiff — never truncated, scrolls if long); the cascade preview; and a text field "Type
`prod` to confirm". The submit button reads "Confirm apply on prod" / "Confirm destroy on staging" and
is enabled only when the typed name matches exactly. The popover is a misclick safeguard, not a
permission check (`can_apply` is still evaluated by the API).

**Banners** (between the bar and the tabs, at most one of each, in this order):
- **Failed during apply** (`failed-tint`, red `✕`): "Failed during apply — infrastructure may be partially changed. First error: …" + "Jump to first error".
- **Failed** (other phases): the run's `error`, plus the failing check's name and output when the error is a bare stage name **[today]**.
- **Stale plan** (neutral, `↑`): when the run is `unconfirmed` and `env.head_sha ≠ run.commit_sha` — "Plan computed on `2af0351`. The branch head is now `125304e` (N commits ahead / ahead)." + **Re-plan on head** (= Discard + new tracked run, two audited calls). Confirming stays possible.
- **Mocked** (`mock-tint`, `◆`): "This plan used mock values for: network_name. A mocked plan cannot be applied here (`allow_mock_apply` is off)." The mocked inputs are listed.
- **Queued** (neutral): the `blocking_reason` from `GET /queue` in words ("waiting: another run is active on this environment → run 01a1276c").

**Tabs** (URL `?tab=`); the default tab depends on the state: Plan for `unconfirmed` and finished
tracked runs, Logs (following) for live runs, Logs at the first error for failed runs.

- **Plan** — `PlanDiff`:
  - Summary line in big mono (`+4 ~0 −1`, zeros neutral), then groups in a fixed order: **DESTROY**, **REPLACE**, CREATE, UPDATE, READ, MOVED, then "**Tag-only updates (n)**" and "No-op (n)" collapsed.
  - **DESTROY and REPLACE groups are always expanded, and cannot be collapsed, filtered out or scrolled away by search** — search highlights in them, filters the others.
  - A resource row: action glyph + label, address in mono, a one-line hint (`2 attributes`, `forces replacement: ami`). Expanded: the attribute diff in `bg-sunken`, one line per attribute (`+ − ~` with diff tokens), `(sensitive value)` as tofu prints it, never a reveal.
  - Tag-only = an update whose changed attributes are all `tags` / `tags_all` (or nested under them).
  - Search (`/`, URL `?plan_q=`) on address and attribute name, with a match counter.
  - **[today]** parsed from the `section="plan"` log block (`tofu show -no-color`): blocks start at `  # <address> will be created | updated in-place | destroyed | must be replaced | read during apply | has moved to`. Anything the parser does not recognise is shown verbatim in a "Unparsed plan output" block **above** the groups, so no line is ever dropped; if the block is missing (older run, failed plan) the tab says so and links to the logs. **[with A1]** from `resource_changes`.
  - Resource rows accept a comment anchored to the resource (`anchor.kind="resource"`, SPECS §16.2).
- **Checks** — one row per check (`results`, SPECS §8.3): status badge, stage, name, duration, output (pre-wrapped mono). Plan-stage `warn` = amber with "requires manual confirmation"; apply-stage `warn` neutral "after confirmation — informational". Tab label carries counts (`2 ok · 1 warn`).
- **Logs** — `LogViewer` (§5.3), filling the viewport height.
- **Inputs** — resolved variables with `ProvenanceBadge`; mocked and fallback values listed first; sensitive values `•••` with no reveal.
- **Discussion** — general threads + resource/line-anchored threads (SPECS §16); open-thread count in the tab label, **neutral** (advisory, SPECS §16.4).

**Next decision**: `]` / `[` and the arrows in the bar walk the user's *needs you* list (same source as
the attention zone), keeping the tab.

**Live updates**: the badge, rail, banners and bar update in place on WS events; a polite live region
announces state changes ("Run is now awaiting decision"); focus never moves.

**< 768 px**: header condensed to two lines; the rail becomes a horizontal strip with labels under the
header; the action bar is docked at the bottom with 44 px buttons (Confirm full width, Discard and
Cancel in an overflow); tabs scroll horizontally; the confirm popover becomes a bottom sheet.

### 5.3 Log viewer (`LogViewer`)

- `bg-sunken`, JetBrains Mono 12/18, virtualized (`react-virtuoso`), ANSI rendered with `anser` mapped onto tokens (red → `state-failed`, green → `state-finished`, yellow → `text-primary` bold — not amber, blue → `state-running`, magenta → `mock`, cyan/white/black → text tokens).
- **Order and sections**: chunks are ordered by lifecycle phase (`planning`, `checking`, `applying`, `running` — the phases of one run never interleave), then `seq`. Not by timestamp: line times are `HH:MM:SS` without a date. (The API orders by phase *name*, so `checking` would come first, AUDIT §6.) Sections = consecutive runs of the same `(phase, section)`: `null` → "tofu", `plan` → "Plan output", `hook:<name>` → "Hook <name>". Section header: chevron, name, line count, duration, and `✕ failed` when a failing check has that hook name. No per-line prefix.
- **Gutter**: line numbers per phase (`planning.87`); click = sets `?line=planning.87` and copies the URL; on arrival the line is highlighted and centred.
- **Follow tail** on by default while the run is live; any upward scroll suspends it and shows "↓ Resume (N new lines)".
- **First error**: lines starting with `Error:` / `│ Error:` or rendered with ANSI red; "Jump to first error" (`e`) and an error count in the toolbar.
- **Search** `⌘F` / `/` when the viewer has focus: highlight, counter, `n` / `N`.
- **Toggles**: timestamps (`t`), wrap (`w`), full screen (`f`), "Save as text" (client-side **[today]**, A10).
- **Streaming**: on a WS signal `{phase, max_seq}` fetch `GET /runs/{id}/logs?phase=&after_seq=<last>` and append; reconnection catches up the same way. Comment-on-line affordance as today (hover / focus a line, `c`).
- **Mobile**: wrap on by default, toolbar collapsed into a menu, sections collapsed except the failing one.

### 5.4 `/s/:space/graph` and run groups

- react-flow, dagre left→right. **Filter** by env name and tier (URL), applied to the graph and the list alike.
- **Node** = `EnvCell` content (stack/env, mini rail, outcome, modifiers) inside a 220 px card. Nodes are focusable in topological order with `aria-label` = env name + summary. Click / Enter opens the **node drawer** (non-modal sheet): env summary, last run (link), published outputs (sensitive masked), dependencies in and out, actions (Open env, Plan).
- **Edge label** = the output names it carries (`network_name`, `+2` when more), from the downstream env's dependency references (fetched lazily per visible edge). Edge style: solid `text-secondary`; **dashed magenta only when the downstream env's primary run consumed a mock for one of the edge's inputs** (`variable_provenance[input] = "mock"`); a reference with a mock *defined* but unused shows a small neutral `◇ mock` marker. Policy shown on hover (`on_output_change`).
- React Flow's controls, labels and background are re-skinned onto tokens (no library default colour).
- **List view** (`?view=list`): the full equivalent — an adjacency table `upstream → downstream · outputs · policy · mock used?` with the same filter, sortable, every row expandable to its references.
- **Run groups**: not available — the API does not expose `run_group_id` (AUDIT A5). **[today]** a run page's cascade preview and the graph highlight "runs started via dependency after this apply" (downstream env runs with `triggered_by=dependency` created after the upstream's `finished_at`), labelled as such. **[with A5]** the real group view: same graph, nodes colouring live, a group header with progress.
- **Auto-link by name** moves to a secondary "Link stacks…" action (writer+) in a dialog.

### 5.5 `/queue` — the execution queue

The visual answer to "why isn't my run starting?". In the nav, with a count badge when a run has
waited more than 2 minutes.

- Three groups: **Executing** (`preparing`, `planning`, `checking`, `applying` — worker name, phase, duration), **Waiting for a worker** (`queued`, `confirmed` — age, blocking reason), and a one-line link "**N runs await a human decision** → Overview" (unconfirmed runs are not in the machine queue).
- Every row resolves names: `stack / env`, run short ID, type, worker name. Ages come from each run's `created_at` (**[today]** one `runs.get` per row; the queue rarely exceeds a few dozen).
- Blocking reasons in words with a link: `another run is active on this environment → run 01a1276c`, `environment locked`, `no worker compatible with labels {…}`, `apply affinity reservation (worker vm, 38 s left)`.
- Filter by stack/env/pool (URL).

### 5.6 `/audit`

- **Admin only** (SPECS §2.4 residual): hidden from the nav for other roles; a direct visit says "The audit journal is restricted to administrators" (AUDIT A9).
- Dense chronological table: mono ISO timestamp (local, with relative on hover), actor (email), action as a neutral badge, **target as a link** (`run 01a1276c · demo-network/prod`, resolved from `target_kind`/`target_id`), context summarised (`+4 ~0 −0`, `tier prod`, `commit 2af0351`).
- `run.confirmed`, `run.applied` and destroys have a slightly contrasted row (`bg-raised`).
- `auth.login` / `auth.refresh` hidden by default ("Show sign-ins" toggle) — they are 90 % of the volume.
- Filters (all in the URL, mapped to the API's `actor`, `action`, `target_kind`, `target_id`, `from_`, `to`): actor, action (grouped select by domain), target (picker: stack, env, run), period. CSV export on the right with the same filters.

### 5.7 Forms, configuration and dangerous switches

- One column, max 560 px, labels above (eyebrow), help in `text-secondary` under the field.
- Every technical name typed (stack, env, variable) is displayed in mono from the first keystroke.
- **Config list items are uniform** (variables, hooks, notifications, secret sources): a bordered row with metadata badges, **Edit** and **Delete** as text buttons (icon + label). Editing is in place. A sensitive value is never pre-filled (write-only): its edit field stays blank with a "leave blank to keep" hint. Secret sources expose **Rotate token** the same way.
- **Friction table** (v2) — the strength of the confirmation follows the blast radius:

| Action | Confirmation |
|---|---|
| Delete a stack, an environment, a worker pool, a variable set, a tier | type the name (dialog) |
| **Turn off** `requires_four_eyes` on a tier, `protected` or `require_second_pair_of_eyes` on an env; turn **on** `allow_mock_apply` / `allow_fallback_apply` | type the tier / env name (dialog that states the consequence: "Members who trigger a prod run will be able to confirm it themselves") |
| Force-unlock a state (admin only; hidden for others) | type the env name; the dialog shows who holds the lock and since when if known |
| Change a user's role, allowed tiers or `can_destroy` | staged edit + "Save" with a summary of the change ("bob: + prod in allowed tiers") |
| Delete a variable, a hook, a notification target, an attachment | confirmation popover anchored to the Delete button, button labelled "Delete variable region" |
| Turning a safety switch **on** (four-eyes on, protected on) | none (one click; reducing risk has no friction) |

- Wizard (new stack): numbered steps; each step independently validatable (the repo check runs at step 1).

### 5.8 Stack page and environment page

**Stack page** `/s/:space/stacks/:stack` — tabs **Environments** and **Settings** (general, variables,
hooks, notifications, secret sources; URL `?tab=`).

- Environments = a table, one row per env: `EnvCell` content, tier, branch, deployed commit, protections (badges: `four-eyes`, `protected`, `autodeploy`, `managed state`), and row actions (Plan, Open). The row opens the env page. "Add environment" (writer+) opens a dialog.

**Environment page** `/s/:space/stacks/:stack/envs/:env` (new in v2):

- **Header**: `stack / env`, tier, branch, deployed commit vs head (`2af0351 → 125304e ↑`, refresh-head button), drift (`≈ drifted, checked 1h ago → drift run`), lock, protection badges.
- **Primary actions** (writer+): **Plan** (primary button), and a "More" menu: Plan with downstream (`with_downstream`), Destroy… (shown only with `can_destroy`; opens a dialog explaining the run will need typed confirmation at apply), Promote from… (sibling env, SPECS §9.7), Run command… (allowlist, SPECS §4.3).
- **Tabs** (URL): **Runs** (default) — history table: run short ID, type (`tracked`, `destroy`, `PR`, `drift`, `command`), trigger (who/how), outcome badge, plan summary, commit, age, duration; filter by type; paginated client-side **[today]**. **Inputs** (Resolved with provenance / Environment overrides / Discover inputs), **Outputs**, **Dependencies**, **State** (versions; force-unlock admin-only per §5.7), **Settings** (name, tier, branch, flags with the §5.7 friction, backend config, labels, hooks, notifications, cloud integration), and a **Danger zone** at the bottom of Settings (delete environment, typed name).

### 5.9 `/workers` — workers & pools

- Health strip: API status, database, workers online/total, runs active/queued, recent warnings — values in `text-primary`; only a failing check is red. No green for "ok" (telemetry is neutral, §3.6).
- Workers grouped by pool: name, labels, status (`online` / `offline` as text + `●` / `○`), last heartbeat (relative), agent version, Diagnostics / Logs actions (admin).
- Pool management (admin): create (token shown **once** in a banner with a copy action, hashed at rest), delete (typed name, §5.7).
- Platform logs: HTTP status and level neutral; `ERROR` / `CRITICAL` and 5xx in red; the worker long-poll on `/worker/v1/jobs/claim` is labelled "long-poll" and excluded from the "slow" highlight.

### 5.10 Variable sets, settings, login, notifications (new section in v2)

- **Variable sets** (`/s/:space/variable-sets`): one row per set with how it reaches envs (`auto-attach`, `rule: team=payments`, `3 attachments`); the row opens a page with tabs Variables / Attachments / Settings. Delete per §5.7.
- **Settings** (`/settings`, admin): **Tiers** (name, `requires_four_eyes` with §5.7 friction, position, envs using it, delete per §5.7), **Users & roles** (staged edits, §5.7), **Spaces** (members, per-space role and tiers), **Deployment** (version, environment).
- **Login**: brand mark, "Sign in with Google"; in dev, the personas show email, role and allowed tiers (they otherwise look alike).
- **Notifications** (bell popover, Radix): each item names what and where ("Run failed · demo-dns/dev · 6m"), links to the run (Logs at first error for failures), mark read on open; "Mark all read" / "Clear read". The bell badge counts unread, neutral (not violet, not amber).

---

## 6. Real-time patterns

- **One multiplexed WebSocket per tab** (a shared hub in `api/stream.ts`): components declare the channels they need (`run:<id>`, `environment:<id>`, `user:<id>`); the hub subscribes/unsubscribes with reference counting. TanStack Query remains the source of truth: WS events **invalidate** queries (they never patch the cache), except logs, which are fetched incrementally with `after_seq` on each signal (§5.3).
- Poll fallbacks only while the socket is down (no permanent 4 s polling).
- A state change is visible everywhere it appears (rail, badges, matrix, attention zone) without a refresh.
- **Connection indicator** in the top bar (`● live`); on a disconnect a thin "Reconnecting…" bar under the top bar, catch-up on reconnect, never a blocking modal.
- No toast for background events (a run that finishes is seen on its rail; the bell receives the notification). Toasts are reserved for feedback on the current user's own actions.

---

## 7. Accessibility, states, quality

- **Contrast**: AA on both themes, verified in §3.1 for every state colour over its real backgrounds; non-text UI ≥ 3:1. Re-checked by a unit test that computes the ratios from `tokens.css`.
- **Colour is never the sole carrier of state**: every badge has its label and glyph; the rail has a glyph per status; diffs have `+ − ± ~`. Colour blindness is the nominal case in this trade.
- **Keyboard**: full navigation with the map of §4.3; visible focus ring everywhere — 2 px `focus` outline, 2 px offset (on fills: 2 px offset ring over a 1 px `bg-base` gap). `prefers-reduced-motion`: the rail's pulse becomes a static opacity change, no transitions.
- **Skip link** "Go to content" as the first focusable element.
- **Live regions, never focus stealing**: one polite region per page for state changes (run state, connection, attention count); action results in a polite region too; focus moves only as a result of the user's own action (opening/closing an overlay returns focus to its trigger).
- **Overlays** are Radix primitives (focus trap in modal dialogs, `Esc`, focus return, `aria-*`).
- **Tables and grids**: real `<table>` semantics for lists; the matrix is `role="grid"` with roving tabindex; sortable headers are buttons with `aria-sort`.
- **Target size**: ≥ 24 × 24 px everywhere (WCAG 2.2 AA), **44 × 44 px** on the on-call mobile path (run page, attention zone, confirm/discard).
- **Tabular figures**: `tabular-nums` on any data aligned in a column.
- **Async action buttons**: `Confirm`/`Discard`/`Cancel`/`Plan`/`Re-plan` go into a `loading` state (spinner + disabled, label kept) during the call — distinct from the permission-disabled state, which carries its reason.
- **Empty states** = invitations to act, in the product's vocabulary, with the action when the user has the right. No decorative illustrations.
- **Errors**: factual, in clear technical English, with the way out; the API's problem `detail` is shown verbatim. Never apologies, never vague.
- **Loading**: skeletons faithful to the final shape for tables, the matrix and the run page; spinners only for one-off actions.
- **Time**: relative under 24 h ("12m ago"), absolute local ISO (`2026-10-10 20:06`) beyond, the other form on hover, always in mono. No locale date formats (`10/10/2026, 8:12 PM`).
- **Responsive**: desktop-first; polished mobile for **the overview's attention zone, reading a run and its logs, and confirming/discarding**. Other screens must not scroll horizontally below 768 px but may simplify.

---

## 8. Implementation

- **Stack** (CLAUDE §3): React 19, Vite 7, strict TypeScript, TanStack Query, Tailwind v4 with the tokens of §3 exposed as theme colours, react-router 7.
- **Dependencies added in the redesign** (agreed): Radix primitives through shadcn/ui, fully re-skinned (Dialog, Popover, DropdownMenu, Tooltip, Tabs, ScrollArea); `cmdk` (⌘K); `react-virtuoso` (logs, long tables); `anser` (ANSI); `@fontsource/ibm-plex-sans`, `@fontsource/jetbrains-mono` (self-hosted fonts). Tooling: `eslint` (flat config) + `typescript-eslint` + `eslint-plugin-react-hooks` + `eslint-plugin-jsx-a11y`, `prettier`. Already present: `@xyflow/react` + `dagre`, `lucide-react`, Ladle.
- **Tokens**: `styles/tokens.css` defines the variables of §3 for `[data-theme="dark"]` and `[data-theme="light"]`; `index.css` maps them into Tailwind (`bg-surface`, `text-secondary`, `text-state-failed`, `border-control`…). **Components use Tailwind utilities only**: no `style={{…}}` except for computed geometry (virtualized heights, graph positions); a lint rule (`no-restricted-syntax` on `style` attributes) and a CI check added in stage 3 (grep for hex/rgb colours, inline colour styles and `localStorage`/`sessionStorage`) enforce it.
- **Theme**: follows `prefers-color-scheme`; the user menu toggle overrides it **in memory** (lost on reload) — persistence needs a profile field (AUDIT A8). `<html data-theme>` is set before first paint by an inline script that reads only `matchMedia`.
- **Folder structure**:
  - `src/ui/` — re-skinned primitives: Button (`primary`, `secondary`, `ghost`, `decision`, `danger`), IconButton, Input, Select, Checkbox, Switch, Field, Tabs, Dialog, Popover, Sheet, DropdownMenu, Tooltip, Command, Table, Badge, Banner (`info`, `decision`, `failed`, `mock`), Kbd, Skeleton, EmptyState, CopyButton, RelativeTime, Sha, TypeToConfirm.
  - `src/identity/` — the identity components, each with a Ladle story (below).
  - `src/features/<domain>/` — `overview`, `runs`, `environments`, `stacks`, `graph`, `queue`, `workers`, `audit`, `variable-sets`, `settings`, `notifications`: page components, their queries and local components. A page file stays under ~250 lines; data hooks live next to it (`useRunPage`, `useOverview`).
  - `src/lib/` — pure, unit-tested logic: `envSummary` (§2.3), `runOutcome` (§3.2), `phases` (§2.2), `permissions` (§5.2 reasons), `planParse` (§5.2), `logModel` (§5.3 ordering/sections/first error), `cascade` (BFS over graph edges), `time`.
  - `src/api/` — client, resources, types (aligned with the API's `RunOut`: `commit_author`, `claimed_at`, `confirmed_at`, `worker_id`, `command`), `stream.ts` hub.
- **Identity components** (contract = Ladle story covering every state, both themes via a theme decorator, and the mocked modifier):

| Component | Props (essential) | Story must show |
|---|---|---|
| `PhaseRail` | `run`, `variant: vertical \| mini`, `onSelect?` | every run state, each failed segment, failed during apply, no changes, auto-confirmed, proposed, command, mocked |
| `StateBadge` | `state`, `outcome?`, `modifiers`, `form?: needsYou \| waiting` | all states, all outcomes, needs you vs waiting, every modifier, stacked modifiers |
| `EnvCell` | `summary` (from `envSummary`), `selected?`, `downstream?` | each primary state, each modifier, empty env, PR active, selected, downstream-highlighted, 390 px |
| `RunActionBar` | `run`, `env`, `tier`, `me`, `cascade`, handlers | needs you, waiting on someone (each reason), four-eyes self-triggered, destroy without right, mocked, fallback, stale, finished applied, failed during apply, loading, API refusal |
| `PlanDiff` | `planText` (today) or `changes` (A1), `query?` | create/update/delete/replace/read/moved, tag-only, long plan (500 resources), unparsed fallback, sensitive values, search |
| `LogViewer` | `chunks`, `live`, `checks`, `initialLine?` | live following, paused with new lines, sections with a failed hook, first error, search, timestamps, ANSI palette, 20 000 lines |
| `ProvenanceBadge` | `provenance` | every kind, mock, fallback, override |
| `TypeToConfirm` | `name`, `summary`, `destroyed[]`, `cascade[]` | four-eyes apply, destroy, long destroy list, mobile sheet |

- **Graphs**: react-flow + dagre, loaded only on `/graph` (route-level code splitting and the `graph` chunk are kept from v1). Initial JS budget: ≤ 250 KB gz.
- **Tests**: vitest for `src/lib/*` (envSummary, runOutcome, phases, permissions, planParse, logModel, cascade, contrast ratios from `tokens.css`) and for identity components; Playwright happy path extended: dev login → overview (attention zone lists a decision) → run page → confirm with typed name → applied outcome; run at 1440 and 390 px. `pnpm lint`, `pnpm typecheck`, `pnpm test` must pass (DEV / CLAUDE §6).

---

## 9. What we forbid ourselves (reminder)

- Drop shadows, gradients, glassmorphism, decorative illustrations, emojis in the UI.
- Summarising an environment by its latest run of any type (§2.3).
- An enabled action that the client already knows the API will refuse; a disabled action without its reason in visible text.
- State colours used for anything other than state; violet anywhere but the logo, focus and selection; amber outside "a human decision is pending" (decision, `unconfirmed`, plan-stage `warn`, FALLBACK) — e.g. for drift, diff updates, log severity or telemetry.
- An icon alone on a destructive action; confirmation without typing the name for deletions, for any apply on a four-eyes env or any `destroy` apply, and for turning a safety switch off (§5.7).
- Hiding, collapsing or filtering out a resource destruction or replacement in a plan.
- Revealing a sensitive value, anywhere, in any form.
- Toasts for background events; unsolicited animations beyond the rail's pulse; moving the user's focus on a real-time event.
- Inline colour styles, hard-coded colours, browser storage.
- Any system text (ID, SHA, value, duration, timestamp) in the UI font: data is mono, always.
