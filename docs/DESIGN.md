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
| 8 | UI text never cites the decision documents (no `§x.y`, `SPECS.md`, `AUDIT A5`): they are for the team. Enforced by lint. | A few helper texts pointed users to SPECS sections. |

What did **not** change: dark-first, density, flat and ruled, "data is mono", the phase rail as
signature, amber = needs a human, magenta = mocked modifier, typed-name friction, destroys never
hidden, no browser storage, tokens only, WS invalidates queries (logs streamed).

Items that need API work are listed in `docs/redesign/AUDIT.md` §6 (A1–A14); every screen below is
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
**[not built yet]** `PhaseRail` renders no cap today: on the run page the outcome is carried by the `StateBadge`
of the action bar, and in a cell by the compact badge next to the mini rail.

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
otherwise `Plan` (a tofu failure before a plan exists), or `Prepare` when no worker ever claimed the
run (`command` runs: `Run` once claimed, else `Prepare`). A failure during Apply is labelled
**"FAILED during apply — infrastructure may be partially changed"**: that sentence is the most
important thing on the page and is never truncated.

**Metadata** under each vertical segment (mono, `text-secondary`): duration computed from the first
and last log line timestamps of that phase **[today]**, exact from run events **[with A7]** — on
Plan, Apply and Run, and only once the phase ran; Plan: the summary `+a ~c −d`; Check: `2 ok · 1 warn`;
Decision: the confirmer's email (`… (rollout approval)` under a rollout, §5.11), `auto`, `discarded`,
`canceled` or `no changes`. A "waiting 12m" on a pending decision is **[not built yet]**.

**Interaction**: each segment with a log phase (Plan, Check, Apply, Run) is a button that opens the
Logs tab at that phase's first line (`?tab=logs&line=planning.1`). Keyboard: segments are in the tab
order after the action bar. Below 1024 px the vertical rail is replaced by a **horizontal** variant
(same segments, labels under a coloured top rule) above the tabs.

**Mini variant**: 5 cells of 10 × 4 px, 2 px gaps, same statuses (pending = 1 px outline, done =
fill, failed = fill + `✕` cap glyph after the rail, waiting = fill amber), followed — in `EnvCell` —
by the compact state badge. `role="img"` with an `aria-label` that reads the outcome: "Unconfirmed, needs your decision, plan +2 ~1 −0".

**Motion**: the active segment pulses (opacity 1 → 0.45, 1.6 s) — the only ambient animation in the
app; `prefers-reduced-motion` replaces it with a static 0.7 opacity.

### 2.3 The truth of an environment (new in v2)

Wherever an environment is summarised — the overview matrix (`EnvCell`), the stack page, the graph
node, the ⌘K results — it is summarised by **one rule**, implemented once (`lib/envSummary.ts`) and
unit-tested:

1. **Primary run** = the environment's **active run** if any: the run holding the env (non-terminal,
   unique per env, SPECS §3.5), else a `queued` one — the uniqueness index excludes `queued`, so a run
   queued behind an unconfirmed plan must never hide that plan. If the primary run is `proposed` it is
   shown with a `PR` marker, since it occupies the env.
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
| `text-primary` | 16.2 / 14.8 / 12.9 / 16.7 | 14.2 / 15.2 / 13.5 / 13.0 |
| `text-secondary` | 7.6 / 7.0 / 6.1 / 7.9 | 5.9 / 6.2 / 5.6 / 5.4 |
| `state-queued` | 7.6 / 7.0 / 6.1 / 7.9 | 5.3 / 5.7 / 5.1 / 4.9 |
| `state-running` | 7.9 / 7.2 / 6.3 / 8.1 | 5.6 / 6.0 / 5.3 / 5.1 |
| `state-decision` | 10.2 / 9.3 / 8.1 / 10.5 | 5.9 / 6.3 / 5.6 / 5.4 |
| `state-finished` | 8.4 / 7.7 / 6.7 / 8.6 | 5.8 / 6.2 / 5.5 / 5.3 |
| `state-failed` | 7.5 / 6.9 / 6.0 / 7.7 | 5.5 / 5.8 / 5.2 / 5.0 |
| `mock` | 7.5 / 6.9 / 6.0 / 7.7 | 6.2 / 6.6 / 5.9 / 5.7 |
| `focus` (non-text, ≥ 3) | 6.9 / 6.3 / 5.5 / 7.1 | 6.0 / 6.3 / 5.6 / 5.4 |
| `border-control` (non-text, ≥ 3) | 4.3 / 3.9 / 3.4 / 4.4 | 3.3 / 3.5 / 3.1 / 3.0 |

On tints, every state colour and both text colours stay ≥ 4.9:1 (dark ≥ 6.2, light ≥ 4.9 — the
lowest is `state-queued` on `failed-tint`, 4.96). `on-fill` on fills (dark / light): on `state-decision` 10.2 / 6.3; on `text-primary` 16.2 / 15.2;
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
| `preparing` `planning` `checking` `confirmed` `applying` `running` | PREPARING / PLANNING / CHECKING / APPLYING / RUNNING (`confirmed` shows APPLYING · STARTING; `running` is a `command` run) | `◐` (pulse) | `state-running` |
| `unconfirmed` | UNCONFIRMED (the SPECS state name, kept for vocabulary consistency; the personal form below says what it means for the viewer) | `⏸` | `state-decision` |
| `failed` | FAILED (+ `at <segment>`), or FAILED DURING APPLY once an apply was decided (§2.2) | `✕` | `state-failed` |
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
| PR | `PR` | `state-queued` (outlined) | the env's active run is a proposed run |

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
(mono labels), 600. Fonts are **self-hosted** (`@fontsource`), Latin subset, only weights Sans 400/600 and
Mono 400/500 imported (`main.tsx`), `font-display: swap`. Preloading and fallback metric overrides
(against layout shift) are **[not built yet]**.

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
| `diff-read` | = `text-secondary` | `<=` / `→` / `·` | READ / MOVE / OTHER |

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
│ ⇧ Rollouts   │                                                           │
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

- **Side nav (208 px, labels always visible — v1 decision kept)**: Overview (solid amber count badge = decisions that need *you*), Graph, Rollouts (§5.11), Queue (neutral outlined count badge = runs waiting > 2 min), Workers (`online/total` in mono, red when none is online or the API is down), Audit (admin only, §5.6), Variable sets, Settings. At the bottom: the **space switcher** (a Radix dropdown listing the space names, the current one marked; hidden when the user sees a single space) and the user block (email, role, allowed tiers) opening a menu with the theme toggle, keyboard shortcuts, single-key shortcuts on/off and sign out. Active item: selection mark (§2.1).
- **Top bar**: entity breadcrumb, connection indicator (`● live` / `◌ reconnecting…`), ⌘K button ("Search ⌘K"), notification bell.
- **Breadcrumb**: built from entities, never from path segments: `space / stack / env / run 01a1276c`. Each segment but the last is a link; the run segment is the current page (its URL is copied with ⌘K "Copy the link to this page").
- **Responsive**: ≥ 1024 px as above. 768–1023 px: the side nav becomes a drawer (a sheet from the right) opened from a menu button in the top bar. < 768 px: a **bottom bar** with three destinations (Overview, Queue, Menu — the Menu opens the same drawer; notifications stay in the top bar at every width) and the run page's action bar docked above it.

### 4.1 URLs (v2)

| Route | Page |
|---|---|
| `/` | redirect to `/s/<space>` — the first space (by name) **that has stacks**, else the first space |
| `/s/:space` | Overview (matrix + attention zone) |
| `/s/:space/stacks/:stack` | Stack page |
| `/s/:space/stacks/:stack/envs/:env` | Environment page (§5.8) |
| `/runs/:runId` | Run page (space resolved from the run) |
| `/s/:space/graph` | Graph |
| `/s/:space/rollouts` | Rollouts list (§5.11) — the side nav's **Rollouts** entry (§4) |
| `/rollouts/:rolloutId` | Rollout page (§5.11; space resolved from the rollout, like the run page) |
| `/s/:space/variable-sets`, `/s/:space/variable-sets/:set` | Variable sets list and one set (§5.10) |
| `/queue`, `/workers`, `/audit`, `/settings` | instance pages |

Names are used instead of UUIDs where they are unique in their parent (space names; stack per space;
env per stack — SPECS constraints). A renamed entity breaks old name-based links, so **v1 routes
stay as redirects** (`/stacks/:uuid` → `/s/<space>/stacks/<name>`; `/stacks`, `/graph` and
`/variable-sets` → the current space's page), and the run URL stays UUID-based (it is the incident link).

**Everything that determines what is on screen is in the URL**: tab (`?tab=plan`), filters (`?q=app&state=attention&tier=prod`), the overview's selected cell and cascade preview (`?sel=demo-network/prod&cascade=1`), log position (`?tab=logs&line=planning.87`), plan search (`?plan_q=nat`), graph filter, view and node (`?env=prod&tier=prod&view=list&node=<id>`), env run-history filter (`?kind=`), stack settings section (`?tab=settings&sub=hooks`). The active space is the URL's, never in-memory state. (No browser storage, CLAUDE §4.8.)

### 4.2 ⌘K command palette

Opened with ⌘K / Ctrl-K from anywhere (`cmdk` on a Radix dialog). Sections, in order **[today]**:
1. **Run** — shown when the query is a full run UUID: open that run.
2. **Go to** — the nav pages (Audit for admins only).
3. **Stacks** and **Environments** (`demo-network/prod`, with the tier) across the user's spaces.
4. **Actions** — Switch to light/dark theme, Copy the link to this page.
5. **Spaces** — switch space (when the user sees more than one).

Specified, **[not built yet]**: a **Needs you** section first (pending decisions the user can act on),
recent runs by short ID, actions on the current page (Plan this env, Plan with downstream, Confirm /
Discard / Cancel — opening the same confirmation as the button, never bypassing it —, Re-plan, Copy
commit SHA), and the env summary glyphs (§2.3) on results. The palette never performs a
state-changing action without the same friction as the button it mirrors.

### 4.3 Keyboard map

| Key | Where | Action |
|---|---|---|
| `⌘K` / `Ctrl-K` | everywhere | palette |
| `g o` / `g q` / `g g` / `g a` | everywhere | go to Overview / Queue / Graph / Audit |
| `?` | everywhere | keyboard help |
| `/` | log viewer (lists, matrix and plan: **[not bound yet]**) | focus the filter / search |
| `j` `k` | matrix rows, attention zone | next / previous row |
| `h` `l` | matrix | previous / next cell in the row |
| `Home` `End` | matrix | first / last cell of the row |
| `Enter` | attention zone, matrix | open (matrix: the env page) |
| `r` / `s` | matrix | open the cell's run / toggle the downstream preview |
| `]` `[` | run page | next / previous run that needs you |
| `1`…`5` | run page | tabs Plan, Checks, Logs, Inputs, Discussion |
| `c` / `d` / `x` | run page | Confirm (opens its friction), Discard, Cancel — only when enabled |
| `f`, `t`, `w`, `e` | log viewer | full screen, timestamps, wrap, jump to first error |
| `n` / `N` | log viewer | next / previous search match |
| `Esc` | overlays | close, focus returns to the trigger |

Single-letter shortcuts are inactive while focus is in a text field or an overlay is open, and can be
disabled from the user menu or Settings → Preferences (WCAG 2.1.4); the setting lives in memory.

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
 h/j/k/l move · ⏎ open · r run · s downstream
```

**Attention zone** (pinned above the matrix, never auto-collapsed while it holds a *needs you* item):

- Groups, in order: **Needs you** (unconfirmed, the user can confirm), **Waiting on someone** (unconfirmed, the user cannot confirm — with the missing condition in words), **Blocked** (unconfirmed, and nobody can confirm: mocked or fallback with apply not allowed on the env), **Failed** (env's primary run failed, during apply or before), **Drift** (`drifted` or check error). A row appears in the first matching group only. A stale plan (unconfirmed run whose commit ≠ env head) is a `↑ stale plan` tag on its row — every unconfirmed run already falls in one of the first three groups.
- Row: badge (compact) + `stack / env` + plan summary + who/age + the reason or next step + Enter target (the run page: Plan tab for decisions, Logs tab — opening at the first error — for failures; for drift, the drift run's Plan tab, else the env page).
- Empty zone: one line, "Nothing needs attention in demo." — not hidden, so its absence is never ambiguous.

**Matrix**:

- Rows = stacks of the space, by name (sorting by "worst state" or last activity is **[not built yet]**). Columns = environment **names** present in the space, ordered by tier `position`, then env `position`, then name; a stack without that env shows `—`. A column header shows the env name, plus `four-eyes` when every env in the column is on a `requires_four_eyes` tier.
- Sticky header row and first column. A wide matrix scrolls horizontally inside its own container, never the page.
- **`EnvCell`** (one per env, 180 px min): line 1 = mini rail + compact state badge (wrapping under the rail when the cell is narrow); line 2 = relative age (+ plan summary when unconfirmed) + modifiers (`↑` `≈` `⚿` `◆` `◈` `PR`). Cell background stays the matrix's `bg-surface`, except `decision-tint` when the cell *needs you*.
- **Selection details**: clicking or focusing a cell selects it (`?sel=stack/env`) and opens a panel under the matrix: `stack / env`, tier, branch, modifiers, deployed commit, branch head, active run or last outcome with who/age, "Confirm needs …" for a pending run, and the actions the user can take (Show downstream, Open run, Open environment, Plan — writer+, when no run is active). A hover popover on the cell is **[not built yet]**.
- **Cascade preview on demand**: `s` (or "Show downstream" in the selection panel; `?cascade=1`) outlines the selected cell's downstream cells (BFS over `GET /graph` edges) with a dashed selection mark and a `↳ will plan` / `↳ plans if outputs change` line, and the panel lists them ("demo-app/prod (if outputs change)"). `s` again or "Hide downstream" clears.
- **Filters** in a top bar, reflected in the URL: text (stack or env name), state (All, Needs attention, Needs you, Running, Failed, Drift, Branch ahead), tier. Filtering by state hides non-matching rows, not cells.
- **Keyboard**: the matrix is a `role="grid"` with roving tabindex (`h/j/k/l`, arrows, `Home`/`End`, Enter, `r`, `s`); the attention zone is a list before it in the tab order.
- **Data** **[today]**: `stacks.list` → `stacks.environments` per stack → `runs.list` per env (the summary needs the active run and the last tracked outcome), one WebSocket subscribed to every `environment:<id>` channel invalidating `["env-runs", id]` (and the stack's environments), plus a 60 s poll. Skeleton blocks while loading. **[with A4]** one request.
- **Empty states**: no stack → "No stack in demo yet. Connect a repository to get started." + New stack (writer+), and the user's other spaces listed as links. The header carries **New environment from…** (§5.11) and **New stack** (writer+).
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

**Header** (identity, read-only): `stack / env` (link to the env page) + run short ID; run type
(a `PR plan` badge for proposed runs, a `− destroy` badge for destroys); commit SHA (copy) + message +
author; branch; how it was triggered (`triggered by bob@…`, `via webhook`, `via dependency`, `via drift
check` **[today]**; the upstream env / run of a cascade `[with A5]`); the rollout and level (§5.11);
age (relative, absolute ISO on hover); total duration; the command and its arguments for a `command`
run. The worker is **[not shown yet]**.

**`RunActionBar`** — sticky under the top bar on desktop, docked at the bottom on < 768 px:

1. **State line**: `StateBadge` (with outcome and modifiers), then one sentence that says what the
   run waits for or how it ended ("Applied 3 changes 2h ago, confirmed by alice", "No changes —
   nothing was applied", "Failed during apply — infrastructure may be partially changed").
2. **Four-eyes line**, whenever four-eyes applies to the env (tier `requires_four_eyes` — fail-closed
   when the tier is missing from the catalog —, `protected`, or `require_second_pair_of_eyes`):
   who triggered, and the consequence. When the run is finished: "Triggered by bob · confirmed by
   alice 12:04".
3. **Your standing** (unconfirmed runs): "You (alice@… · approver · dev staging prod) can confirm" /
   "cannot confirm" — the reasons are listed under the bar. **[today]** computed from `/auth/me`,
   labelled "Based on your instance permissions" when the user sees more than one space (per-space
   memberships the client cannot read); **[with A2]** exact. "Who may still confirm" is stated as a
   rule (**[today]**) — names come with A3.
4. **Cascade preview** (unconfirmed runs): downstream envs from `GET /graph` (BFS): `always` → "will
   plan", `on_output_change` → "if <outputs> change", `never` → omitted. Mock edges are marked in the
   confirm popover ("◆ mock defined").
5. **Actions**, right-aligned, in this order: Cancel run / Stop run / Stop apply · Discard ·
   **Confirm** (amber — the `decision` button variant, also used only by a rollout's Approve and by the
   typed-name submit that turns a safety switch off, §5.7) · Plan again (terminal runs, desktop) ·
   next-decision arrows (desktop).

**Action availability and disabled reasons** — an action that does not apply to the run's state is
**not rendered**; an action that applies but is refused is rendered **disabled with its reason as
visible text** under the bar (and as `aria-describedby`), never only in a tooltip:

| Action | Rendered when state is | Disabled when (reason text) |
|---|---|---|
| Confirm | `unconfirmed` | account disabled ("your account is disabled"); role not approver/admin ("approver role required"); no allowed tiers ("no allowed tiers — you cannot confirm applies"); `env.tier ∉ allowed_tiers` ("tier prod is not in your allowed tiers (dev, staging)"); `type=destroy` and not `can_destroy` ("destroy permission required"); `used_mocks` and not `allow_mock_apply` ("mocked inputs — apply is disabled on this environment (allow_mock_apply is off)"); `used_secret_fallback` and not `allow_fallback_apply` ("a secret came from a fallback — apply is disabled (allow_fallback_apply is off)"); four-eyes and `trigger_user_id = you` with `triggered_by=manual` ("you triggered this run; four-eyes requires someone else to confirm") |
| Discard | `unconfirmed` | role reader ("writer role required") — SPECS §2.3 |
| Cancel run | `queued`, `confirmed`, `unconfirmed` — ends the run at once | role reader |
| Stop run | `preparing`, `planning`, `checking`, `running` (a read-only command) — asks the worker to stop the tool (SPECS §4.1); the run ends when the worker reports; "Stop again" once requested | role reader |
| Stop apply | `applying`, or a `running` mutating command — **two clicks**: the first turns the label into "Stop the apply? It may be left partly applied — click again" for 6 s | role reader, plus the apply right (role, tier, destroy — not the mock/fallback/four-eyes gates) |
| Plan again / Re-plan on head | any terminal state ("Plan again", in the bar, desktop); `unconfirmed` with a stale plan ("Re-plan on head", in the stale banner) — `tracked` and `destroy` runs only | role reader |

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
- **Stop requested** (neutral): while an executing run has `cancel_requested_at` — "Stop requested — waiting for the worker." and what the worker does (interrupts the tool as a Ctrl-C would; the run ends when the worker reports it).
- **Failed during apply** (`failed-tint`, red `✕`): "Failed during apply — infrastructure may be partially changed." + the run's error + "Jump to first error". A stop during apply (`error = canceled_during_apply`) reads "Stopped during apply on request — infrastructure may be partially changed." with "Review what was applied before planning again".
- **Failed** (other phases): "Failed at <segment> — nothing was applied." + the run's `error`, or the failing check's name, stage and output when there is one **[today]**; "Jump to first error".
- **Stale plan** (neutral, `↑`): when the run is `unconfirmed` and `env.head_sha ≠ run.commit_sha` — "Plan computed on `2af0351`. The branch head is now `125304e` (N commits ahead / ahead)." + **Re-plan on head** (= Discard + new tracked run, two audited calls). Confirming stays possible and still applies the planned commit.
- **Mocked** (`mock-tint`, `◆`): "This plan used mock values — it is not real." The mocked inputs are listed, then "A mocked plan cannot be applied here (allow_mock_apply is off)" or that the env allows it.
- **Fallback** (`decision` tone, `◈`): "A secret came from a fallback, not the real source." + whether `allow_fallback_apply` lets it through.
- **Queued** (neutral): the `blocking_reason` from `GET /queue` in words ("Waiting: another run is active on this environment.") + a link to the queue.

**Tabs** (URL `?tab=`); the default tab depends on the state: Plan for `unconfirmed` and finished
tracked runs, Logs (following) for live runs, Logs at the first error for failed runs.

- **Plan** — `PlanDiff`:
  - Summary line in big mono (`+4 ~0 −1`, zeros neutral), then groups in a fixed order: **DESTROY**, **REPLACE**, CREATE, UPDATE, READ, MOVE, OTHER (e.g. "will no longer be managed"), then "**Tag-only updates · n**" and "Changes to outputs" collapsed, the tool's summary line, and the plan legend as printed (collapsed).
  - **DESTROY and REPLACE groups are always expanded, and cannot be collapsed, filtered out or scrolled away by search** — search highlights in them, filters the others.
  - A resource row: action glyph + label, address in mono, a one-line hint (`2 attributes`, `forces replacement: ami`). Expanded: the attribute diff in `bg-sunken`, one line per attribute (`+ − ~` with diff tokens), `(sensitive value)` as tofu prints it, never a reveal.
  - Tag-only = an update whose changed attributes are all `tags` / `tags_all` (or nested under them).
  - Search (`/`, URL `?plan_q=`) on address and attribute name, with a match counter.
  - **[today]** parsed from the `section="plan"` log block (`tofu show -no-color`): blocks start at `  # <address> will be created | updated in-place | destroyed | must be replaced | read during apply | has moved to`. Anything the parser does not recognise is shown verbatim in a "Unparsed plan output" block **above** the groups, so no line is ever dropped; if the block is missing (older run, failed plan) the tab says so and links to the logs. **[with A1]** from `resource_changes`.
  - Resource rows accept a comment anchored to the resource (`anchor.kind="resource"`, SPECS §16.2).
- **Checks** — one row per check (`results`, SPECS §8.3; older runs fall back to `checks`): status badge, stage (`—` when not recorded), name, output (pre-wrapped mono). A plan-stage `warn` adds "manual confirmation required" in amber; an apply-stage `warn` adds "after confirmation — informational", neutral. Tab label carries counts (`2 ok · 1 warn`).
- **Logs** — `LogViewer` (§5.3), 70 vh tall.
- **Inputs** — each input's name with its `ProvenanceBadge`; values that are not the real thing (mock, fallback, override) listed first. Values are not kept on the run, so none is shown; sensitive values are never shown anywhere.
- **Discussion** — general threads + resource/line-anchored threads (SPECS §16); open-thread count in the tab label, **neutral** (advisory, SPECS §16.4).

**Next decision**: `]` / `[` and the arrows in the bar walk the user's *needs you* list (same source as
the attention zone), keeping the tab.

**Live updates**: the badge, rail, banners and bar update in place on WS events; a polite live region
announces state changes ("Run is now applying."); focus never moves.

**< 768 px**: the action bar is docked at the bottom with 44 px buttons (Confirm stretches, Discard
and Cancel/Stop stay visible beside it; Plan again and the next-decision arrows are hidden); the
confirm popover becomes a bottom sheet; tabs scroll horizontally. Below 1024 px the rail is the
horizontal strip above the tabs (§2.2). A condensed two-line header is **[not built yet]**.

### 5.3 Log viewer (`LogViewer`)

- `bg-sunken`, JetBrains Mono 12 px on 24 px rows (§7 target size), virtualized (`react-virtuoso`), ANSI rendered with `anser` mapped onto tokens (red → `state-failed`, green → `state-finished`, yellow → `text-primary` bold — not amber, blue → `state-running`, magenta → `mock`, cyan/white → `text-primary`, black → `text-secondary`).
- **Order and sections**: chunks are ordered by lifecycle phase (`preparing`, `planning`, `checking`, `applying`, `running` — the phases of one run never interleave), then `seq`. Not by timestamp: line times are `HH:MM:SS` without a date. (The API orders by phase *name*, so `checking` would come first, AUDIT §6.) Sections = consecutive runs of the same `(phase, section)`: `null` → the phase's name ("Prepare", "Init & plan", "Checks", "Apply", "Command"), `plan` → "Plan output", `hook:<name>` → "Hook · <name>". Section header: chevron, name, line count, duration, and `✕ failed` when the section holds an error line or a failing check has that hook name. No per-line prefix.
- **Gutter**: line numbers per phase (`planning.87`); click = sets `?line=planning.87` and copies the URL; on arrival the line is highlighted and centred.
- **Follow tail** on by default while the run is live; any upward scroll suspends it and shows "Resume following (N new lines)".
- **First error**: lines starting with `Error:` / `│ Error:` (ANSI codes allowed before it); "First error (n)" (`e`) in the toolbar, and a `✕ n` count on the Logs tab. A failed run opens the Logs tab at its first error, else at its failing hook's section.
- **Search** `⌘F` / `/` when the viewer has focus: highlight, counter, `n` / `N` (Enter / Shift-Enter in the field).
- **Toggles**: timestamps (`t`), wrap (`w`, on by default), full screen (`f`), "Save" — a client-side text file of the loaded lines **[today]**. The API now serves `GET /runs/{id}/logs/download` (A10) from either storage tier; the viewer does not use it yet.
- **Streaming**: on a WS signal `{phase, max_seq}` fetch `GET /runs/{id}/logs?phase=&after_seq=<last>` and append (one fetch in flight per phase). Logs of a run archived to object storage are served by the same endpoint, transparently. Comment-on-line affordance: a comment button revealed on hover / focus of a line (always visible on coarse pointers).
- **Mobile** **[not built yet]**: toolbar collapsed into a menu, sections collapsed except the failing one.

### 5.4 `/s/:space/graph` and run groups

- react-flow, dagre left→right. **Filter** by env name and tier (URL), applied to the graph and the list alike.
- **Node** = `EnvCell` content (stack/env, mini rail, outcome, modifiers) inside a 230 px card. Nodes are focusable in topological order with `aria-label` = env name + summary. Click / Enter opens the **node drawer** (non-modal sheet): env summary, last run (link), published outputs (sensitive masked), dependencies in and out, actions (Open env, Plan).
- **Edge label** = the output names it carries (the first two, then `+n`), from the downstream env's dependency references (fetched lazily per visible edge). Edge style: solid `text-secondary`; **dashed magenta, labelled `◆ mock`, only when the downstream env's primary run consumed a mock for one of the edge's inputs** (`variable_provenance[input] = "mock"`); a reference with a mock *defined* but unused adds a small neutral `◇` to the label. The policy (`on_output_change`) is shown in the list view and the node drawer.
- React Flow's controls, labels and background are re-skinned onto tokens (no library default colour).
- **List view** (`?view=list`): the full equivalent — an adjacency table `upstream → downstream · outputs · policy · mock used?` with the same filter, sortable, every row expandable to its references.
- **Run groups**: the API now exposes `run_group_id`, `parent_run_id` and `run_group {id, status}` on `GET /runs/{id}` (SPECS §9.2, AUDIT A5); the front does not use them yet. **[today]** a run page's cascade preview and the graph highlight "runs started via dependency after this apply" (downstream env runs with `triggered_by=dependency` created after the upstream's `finished_at`), labelled as such. **[with A5]** the real group view: same graph, nodes colouring live, a group header with progress.
- **Auto-link by name** moves to a secondary "Link stacks…" action (writer+) in a dialog.

### 5.5 `/queue` — the execution queue

The visual answer to "why isn't my run starting?". In the nav, with a count badge when a run has
waited more than 2 minutes.

- Three groups: **Executing** (`preparing`, `planning`, `checking`, `applying`, `running` — worker name, phase, duration), **Waiting for a worker** (`queued`, `confirmed` — age, blocking reason), and a one-line link "**N runs await a human decision** → Overview" (unconfirmed runs are not in the machine queue).
- No WebSocket channel carries the queue: the page polls `GET /queue` every 3 s (the nav badge every 15 s).
- Every row resolves names: `stack / env`, run short ID, type, worker name. Ages come from each run's `created_at` (**[today]** one `runs.get` per row; the queue rarely exceeds a few dozen).
- Blocking reasons in words with a link: `another run is active on this environment → run 01a1276c`, `environment locked`, `no worker compatible with labels {…}`, `apply affinity reservation (worker vm, 38 s left)`.
- Filter by stack/env/pool (URL).
- When the run holding the environment is an unconfirmed plan, the reason says so ("a plan on this environment awaits a human decision → run …"): the wait is on a person, not a machine. A claimable run reads "claimable — waiting for the next worker poll", or "no worker is online → workers" when `/health` reports none (needs API work A11 for the exact reason).
- On phones each run is a card with the same labelled fields as the table columns.

### 5.6 `/audit`

- **Admin only** (SPECS §2.4 residual): hidden from the nav for other roles; a direct visit says "The audit journal is restricted to administrators" (AUDIT A9).
- Dense chronological table: mono ISO timestamp (local, with relative on hover), actor (email), action as a neutral badge, **target as a link** (`run 01a1276c · demo-network/prod`, resolved from `target_kind`/`target_id`), context summarised (`+4 ~0 −0`, `tier prod`, `commit 2af0351`). A system `run.confirmed` under a rollout's approval reads "auto-confirmed under the rollout approval of alice@…" (SPECS §21.5) — never as alice's confirm. The CSV export carries the context as a JSON column.
- `run.confirmed`, `run.applied` and destroys have a slightly contrasted row (`bg-raised`).
- `auth.login` / `auth.refresh` / `auth.logout` hidden by default ("Show sign-ins" toggle) — they are 90 % of the volume. Older events load on demand ("Load older events").
- Filters (all in the URL, mapped to the API's `actor`, `action`, `target_kind`, `target_id`, `from_`, `to`): actor, action (grouped select by domain), target (picker: stack, env, run), period. CSV export on the right with the same filters.

### 5.7 Forms, configuration and dangerous switches

- One column, max 560 px, labels above (eyebrow), help in `text-secondary` under the field.
- Every technical name typed (stack, env, variable) is displayed in mono from the first keystroke.
- **Config list items are uniform** (variables, hooks, notifications, secret sources): a bordered row with metadata badges, **Edit** and **Delete** as text buttons (icon + label). Editing is in place. A sensitive value is never pre-filled (write-only): its edit field stays blank with a "leave blank to keep" hint. Secret sources expose **Rotate token** the same way.
- **Friction table** (v2) — the strength of the confirmation follows the blast radius:

| Action | Confirmation |
|---|---|
| Delete a stack, an environment, a worker pool, a variable set, a tier | type the name (dialog) |
| **Turn off** `requires_four_eyes` on a tier, `protected` or `require_second_pair_of_eyes` on an env; turn **on** `allow_mock_apply` / `allow_fallback_apply` on an env or `repo_hooks_at_apply` on a tier | type the tier / env name (dialog that states the consequence: "Members who trigger a prod run will be able to confirm it themselves"); its submit is the amber `decision` button |
| Force-unlock a state (admin only; hidden for others) | type the env name; the dialog shows who holds the lock and since when if known |
| Change a user's role, allowed tiers or `can_destroy` | staged edit + "Save" with a summary of the change ("bob: + prod in allowed tiers") |
| Delete a variable, a hook, a notification target, an attachment | confirmation popover anchored to the Delete button, button labelled "Delete variable region" |
| Approve a rollout (SPECS §21.6) | dialog listing every member env with its tier and the gate ("creates-only plans will be applied without asking again, for 24 h"); type the rollout's environment name |
| Turning a safety switch **on** (four-eyes on, protected on, repo hooks at apply forbidden) | none (one click; reducing risk has no friction) |

- Wizard (new stack): numbered steps; each step independently validatable (the repo check runs at step 1).

### 5.8 Stack page and environment page

**Stack page** `/s/:space/stacks/:stack` — tabs **Environments**, **Interface** and **Settings** (sections general,
variables, hooks, notifications, secret sources; URL `?tab=settings&sub=`).

- Environments = a table, one row per env: the env name (link to the env page), `EnvCell`, tier, branch, deployed commit, protections (badges: `four-eyes`, `protected`, `2nd eyes`, `autodeploy`); branch, deployed commit and protections are hidden below 768 px. Row actions (Plan, Open) are **[not built yet]**. "Add environment" (writer+) opens a dialog.

- **Interface** (SPECS §24, `?tab=interface&ref=`) = the stack's *declared interface*: a ref picker (default branch, then the branches its environments track), a provenance line in mono — "Declared at `125304e` on `main` · infra/app" — then two tables. **Inputs**: name (mono), type (mono, `any` when untyped), a `required` badge or the default (mono, truncated; sensitive → `•••`, never revealable), a `sensitive` badge, description. **Outputs**: name, `sensitive` badge, description. Warnings (unparsed `.tf.json`, a file that failed to parse) as a neutral notice above the tables: the interface may be incomplete. Copy says "declared", never "current values". Empty states: "This root module declares no variables." / "…no outputs."

**Environment page** `/s/:space/stacks/:stack/envs/:env` (new in v2):

- **Header**: `stack / env`, badges (tier, `four-eyes`, `protected`, `second pair of eyes`, `autodeploy`, `managed state`); a summary strip with the env's `EnvCell` (active run or last outcome), branch, deployed commit and head (`↑` when ahead; a Refresh button, writer+), drift (`≈ DRIFT` + "see the drift" → drift run, or "No drift detected · checked 1h ago" / "Drift detection off"), lock.
- **Primary actions** (writer+): **Plan** (primary button), and a "More" menu: Plan with downstream (`with_downstream`), Promote from a sibling… (SPECS §9.7), Run a command… (allowlist, SPECS §4.3), New environment from… (this env preselected as the model), Roll out every “<name>” environment… (SPECS §21, DESIGN §5.11) and Plan a destroy… (shown only with `can_destroy`; opens a dialog explaining the run will need typed confirmation at apply).
- **Tabs** (URL): **Runs** (default) — history table: run short ID, kind (`tracked`, `destroy`, `PR`, `drift`, `command`), outcome badge, mini rail, commit, trigger (who/how), start, duration; filter by kind (`?kind=`); paginated client-side, 50 at a time **[today]**. **Inputs** (Declared coverage / Resolved with provenance / Environment overrides / Discover inputs), **Outputs**, **Dependencies**, **State** (versions; force-unlock admin-only per §5.7), **Settings** (name, tier, branch, flags with the §5.7 friction, backend config, labels, hooks, notifications, cloud integration), and a **Danger zone** at the bottom of Settings (delete environment, typed name).
- **Declared coverage** (SPECS §24.2), at the top of **Inputs**: "Declared at `125304e`" (the env's head; the head *Refresh* button of the header is how it moves), then one row per declared variable — name, status with label + icon, never color alone (`✓ set` + `ProvenanceBadge`, `○ default`, `! missing` with "a plan will fail" and, for writer+, the Discover inputs action), type — and below a collapsed "Configured but not declared · n" (env/stack-level ones flagged, variable-set ones muted). **Outputs** tab: captured outputs as today, each marked `declared` or `no longer declared`; declared outputs never captured yet listed after them as "declared, not applied yet".

### 5.9 `/workers` — workers & pools

- Health strip: API status, database, workers online/total, runs active/queued, recent warnings — values in `text-primary`; only a failing check is red. No green for "ok" (telemetry is neutral, §3.6).
- Workers grouped by pool (`n/m online`, pool labels): name, status (`● online` neutral, `✕ offline` in red — an error), last heartbeat (relative), labels, runner (`docker` / `local`), agent version, Diagnostics / Logs actions (admin).
- Pool management (admin): create (token shown **once** in a banner with a copy action, hashed at rest), delete (typed name, §5.7).
- Platform logs (admin): HTTP status and level neutral; `ERROR` / `CRITICAL` and 5xx in red; the worker long-poll on `/worker/v1/jobs/claim` is labelled "long-poll" and excluded from the "slow" highlight.

### 5.10 Variable sets, settings, login, notifications (new section in v2)

- **Variable sets** (`/s/:space/variable-sets`): one row per set with how it reaches envs (`auto-attach · whole space`, `rule: team=payments`, `3 attachments`); the set opens its page (`/s/:space/variable-sets/:set`) with tabs Variables / Attachments / Settings (`?tab=`). Delete per §5.7.
- **Settings** (`/settings`): **Preferences** for everyone (theme: follow the system / dark / light; single-key shortcuts on/off — in memory, for the tab only, until A8) and **Deployment** for everyone (environment, version, database status). Admin only: **Tiers** (name, `requires_four_eyes` and `repo_hooks_at_apply` with §5.7 friction, display order, envs using it, delete per §5.7), **Users** (role, allowed tiers, `can_destroy`, disabled — staged edits, §5.7; a filter Everyone / People / Service accounts; a service account carries a `service account` badge, is offered only `reader` / `writer` as instance role and no tier or destroy right — it never applies, SPECS §23), **Service accounts** (SPECS §23: list with role, `disabled`, active token count; "New service account" dialog — name, reader/writer; per account a **Tokens…** dialog listing name, prefix, status badge `active` / `expired` / `revoked`, expiry, last use, with **New token** — name, 1–365 days — whose raw value is shown **once** in a read-only field with a copy action and "you won't see it again", held in the dialog's state only and dropped when it closes; **Revoke…** behind a confirmation popover; **Disable…** behind a popover, Enable in one click; the section says plainly that a service account plans but never confirms an apply), **Spaces** (members, per-space role and tiers; the add picker groups People and Service accounts). For a **space admin** (an instance admin, or anyone whose space membership list answers): **Space configuration** (SPECS §22) — pick a space, **Export configuration** (a JSON download through an object URL), **Import configuration…** (dialog: pick a `.json` file, read in memory; **Run a dry run** → report: counts, secrets to fill first in an amber banner, warnings, skipped with the reason in words, changes grouped by op then kind with the changed fields, unchanged collapsed; **Apply import** only after a dry run → the final report, a created worker pool's token shown once like an API token; a 422 lists the fields at fault, never their input).
- **Login**: brand mark, "Sign in with Google"; in dev, the personas show email and role, and their allowed tiers once the dev endpoint returns them (needs API work A12).
- **First-login walkthrough**: a short dialog tour (overview, stacks and environments, runs, permissions, dependencies…) shown until the user completes it; closing it in any way completes it, persisted server-side (no browser storage).
- **Notifications** (bell popover, Radix): each item names what and where ("Run failed · demo-dns/dev · 6m"), links to the run (Logs at first error for failures) — a rollout notification (`rollout_paused`, `rollout_succeeded`, `rollout_failed`, `rollout_upstream_changed` "Upstream applied under a rollout", SPECS §21.7) to the rollout page. Opening the popover marks every item read (items that were unread keep a "new" mark while it stays open); "Clear read" removes the read ones. The bell badge counts unread, neutral (not violet, not amber).

### 5.11 Environment clones and rollouts (SPECS §21)

**New environment from…** (overview header action, and the env page "More" menu with that env
preselected as the model — the same label in both places; writer+): a dialog, one column (§5.7).
- Fields: reference environment name (select of the names present in the space), new name (mono),
  stacks (checklist, default every stack with the reference — the ones without it listed, disabled,
  with the reason; at least one must stay checked), overrides (tier, branch, labels as `key=value`,
  comma-separated — the value is everything after the first `=`; a malformed pair is shown inline
  under the field and blocks Review, never dropped silently), "Copy cloud integration" (admin only).
- **Live hint** under the labels (a debounced `dry_run` of the request as typed, SPECS §21.2): which
  variable sets the labels will attach ("new" in words when the model does not get it, the stacks
  named when not all), the model's items **not copied** because they would shadow a key of such a set
  (variable or set attachment, the key and the set named), and the envs whose **autodeploy is forced
  off** (with the reason: protected, or secrets/settings left to fill). Text only — no colour carries it.
- **Preview before create** (a `dry_run` of the same request): first, the response's `warnings` as
  an `info` banner "Check before creating", one line each starting with "Warning:" — an edge from the
  model of a stack left out of the checklist ("app/prod-euw3 will depend on network/prod — the model
  itself, in a stack not cloned, not a shared environment"), a tier override away from the model's
  tier (from → to, and the secret references, set attachments and cloud integration it keeps). Then per
  stack, what is copied and what is
  not (§21.2 table, as text, plus the hint above), the dependency edges that will be created
  (upstream → downstream, marked "shared upstream" or "model of a stack not cloned" in words — never
  the same label for both), and what the user will still have to fill
  (sensitive variables — each with the lower-scope value it resolves to meanwhile, when there is one
  — static fallbacks of secret references, cloud integration). Create = one request.
- After create: a summary with the `missing_secrets` as a checklist linking to each env's Inputs tab,
  and the primary button **Roll out prod-euw3…**, which creates the rollout (awaiting approval) and
  opens its page. The env page's "More" menu has the same action ("Roll out every `<name>`
  environment…").

**Rollouts list** `/s/:space/rollouts` (side nav): one row per rollout, state badge, `done / members`.
On the overview, a banner per rollout awaiting approval, paused or running, linking to its page.

**Rollout page** `/rollouts/:id` (space-less, like `/runs/:id`; linked from the overview banners, the
list, the notifications and every member run):
- **Header**: environment name, state badge (`StateBadge` vocabulary: awaiting approval = decision
  amber, running, paused = decision amber, succeeded, failed, canceled), creator, approver and approval expiry
  ("approved by alice · expires in 21 h"), actions: **Approve…** / **Approve again…** — shown when
  the API says an approval is what the rollout waits for (`approvable`: the first approval, or a
  re-approval after an expiry, a lost right or a member changed since the approval); replaced, for
  a viewer who may not approve, by the reason (four-eyes against the creator, a tier they lack) —,
  Retry (failed), Cancel. The approval dialog (§5.7 friction) says what it lets through: plans that
  only create, in environments never applied, on the commit the first plan of each stack fixes;
  everything else stops for a human.
- **Paused banner**: the pause reason in words — "a plan needs a human confirmation", "the approval
  expired: approve again to go on", "the approver may no longer apply on a member: approve again",
  "a member's tier, protection or four-eyes changed since the approval: approve again".
- **Body**: the members as **columns per level** (an ordered list of levels, left → right, each a
  list of member cards — the accessible structure is the markup itself): env link, tier and
  `protected`, status badge (label + glyph, never colour alone), the plan summary (`+2 ~0 -0`, `⇣n`
  for imports, any other kind by name: `forget 1`), the run link, **why it is waiting** when it
  stopped ("changes 1 resource", "forgets 1 resource", "consumed a mock", "check warn: infracost",
  "the environment already has infrastructure", "planned commit …, not … the rollout pinned", "its
  tier, protection or four-eyes changed since the approval"), "Confirmed under the rollout approval" for an auto-confirmed
  member, and, before it plans, "Will plan on a mock (…)" when an outside upstream lacks an output.
  The react-flow graph of §5.4 is not reused here.
- Before approval, the page *is* the preview (§21.4): members by level, tiers, and the members
  expected to pause.

**Run page**: a member run carries, in its header line, "part of rollout prod-euw3 · level 2" (a
link), and, when auto-confirmed (`confirmed_via: rollout` on the run, SPECS §21.5 — also for an
earlier run of a retried member), the action bar's who/when reads "confirmed by alice@… (rollout
approval)" — never a plain "confirmed by alice": she approved the rollout, not this plan.

---

## 6. Real-time patterns

- **One multiplexed WebSocket per tab** (a shared hub in `api/stream.ts`): components declare the channels they need (`run:<id>`, `environment:<id>`, `user:<id>`, `rollout:<id>`); the hub subscribes with reference counting and re-subscribes after a reconnect (the server has no unsubscribe: a released channel stays on the socket until the next reconnect, and listeners filter signals on their payload). The token is sent as the first message, never in the URL; reconnects back off exponentially up to 30 s. TanStack Query remains the source of truth: WS events **invalidate** queries (they never patch the cache), except logs, which are fetched incrementally with `after_seq` on each signal and merged into the log query (§5.3).
- Slow safety-net polls run alongside the socket (60 s on the overview, stack and env pages, 15 s on a live run) — no fast permanent polling on socket-backed pages. Pages with no channel poll: the queue (3 s), workers health (5 s).
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
  - Below 768 px, form fields and `md` buttons are 44 px tall and fields use 16 px text (below that, iOS zooms into a focused field); checkboxes grow to 20 px.
  - A link standing alone in a table cell or list row uses `.cell-link` (24 px hit area), so adjacent rows never share a target.
  - Log rows are 24 px tall, so the line-number and comment targets meet 24 px with any pointer (density traded for target size).
  - Nothing is revealed on hover alone for touch: hover-only actions are always visible on coarse pointers.
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
- **Dependencies added in the redesign** (agreed): Radix primitives (Dialog, Popover, DropdownMenu, Tooltip, Tabs), wrapped and fully re-skinned in `src/ui/` **the shadcn/ui way** — owned code, Tailwind utilities on the tokens, a small `cn()` helper — but written by hand, not generated by the shadcn CLI (no `components.json`, no `class-variance-authority` / `tailwind-merge`); scrolling stays native (no ScrollArea); `cmdk` (⌘K); `react-virtuoso` (the log viewer); `anser` (ANSI); `@fontsource/ibm-plex-sans`, `@fontsource/jetbrains-mono` (self-hosted fonts). Tooling: `eslint` (flat config) + `typescript-eslint` + `eslint-plugin-react-hooks` + `eslint-plugin-jsx-a11y`, `prettier`. Already present: `@xyflow/react` + `dagre`, `lucide-react`, Ladle.
- **Tokens**: `styles/tokens.css` defines the variables of §3 for `[data-theme="dark"]` and `[data-theme="light"]`; `index.css` maps them into Tailwind under distinct utility names, since `@theme` re-emits its variables and a same-name alias would be a cycle (`bg-base`/`surface`/`raised`/`sunken`, `border-line`, `border-control`, `bg-scrim`, `text-primary`, `text-muted` = text-secondary, `ring`, `logo`, `text-onfill`, `text-st-<state>`, `text-mocked`, `bg-tint-<state>`, `text-df-<action>`…), plus the type scale (`text-eyebrow` … `text-display`) and radii (`rounded-base`, `rounded-badge`). **Components use Tailwind utilities only**: no `style={{…}}` except for computed geometry (virtualized heights, graph positions). Enforced by a lint rule (`no-restricted-syntax` on colour properties inside `style` attributes, plus `no-restricted-globals` / `no-restricted-properties` on `localStorage` / `sessionStorage`) and by `pnpm check:invariants` in CI (`front/scripts/check-invariants.mjs`: no browser storage, no hex / `rgb()` / `hsl()` literal outside `tokens.css`, no `text-base` — a colour here, not a size).
- **Theme**: follows `prefers-color-scheme`; the user menu toggle, Settings → Preferences or ⌘K overrides it **in memory** (lost on reload) — persistence needs a profile field (AUDIT A8). `<html data-theme>` is set before first paint by an inline script that reads only `matchMedia`.
- **Folder structure**:
  - `src/ui/` — re-skinned primitives: Button (`primary`, `secondary`, `ghost`, `decision`, `danger`; sizes `sm`, `md`, `touch`), Input, NativeSelect, Checkbox, Field, Tabs, Table (`Th`, `Td`), Badge, Banner (`info`, `decision`, `failed`, `mock`), Kbd, Skeleton, EmptyState, CopyButton, RelativeTime, Sha (`primitives.tsx`); Dialog, Sheet, Popover, Menu (DropdownMenu), Tooltip (`overlays.tsx`); ConfirmPopover, TypeNameDialog; OneTimeSecret (a credential the API returns once: read-only field, copy, "you won't see it again"), ProblemBanner (an API refusal: detail, and a 422's fields at fault); `cn`, `useMediaQuery`. No IconButton or Switch primitive (icon-only tools are `Button`s with an `aria-label`); ⌘K uses `cmdk` directly in `app/CommandPalette.tsx`.
  - `src/identity/` — the identity components, each with a Ladle story (below), plus `TypeToConfirm` and the shared story fixtures.
  - `src/app/` — the shell: routes and nav (`AppShell`, `nav.ts`), space context, ⌘K, keyboard map, theme, live-region announcer, notification bell, login, walkthrough, brand.
  - `src/features/<domain>/` — `overview`, `runs`, `environments`, `stacks`, `graph`, `queue`, `workers`, `audit`, `variableSets`, `settings`, `rollouts` (rollouts and the clone dialog), `data` (`useSpaceEnvs`, the space-wide env summaries shared by the overview, the graph, the run page and the nav badge): page components, their queries and local components. A page file stays under ~250 lines; data hooks live next to it (`useRunPage`, `useSpaceEnvs`).
  - `src/components/` — configuration panels kept from v1 (variables, hooks, notifications, secret sources, cloud, dependencies, promote, command, stack general) and their small `ui.tsx` kit (`DeleteButton`, `ItemTile`, `LabelsEditor`…), restyled onto the tokens.
  - `src/lib/` — pure, unit-tested logic: `envSummary` (§2.3), `runOutcome` (§3.2), `phases` (§2.2), `permissions` (§5.2 reasons), `planParse` (§5.2), `logModel` (§5.3 ordering/sections/first error), `cascade` (BFS over graph edges), `time`.
  - `src/api/` — client, resources, types (aligned with the API's `RunOut`: `commit_author`, `claimed_at`, `confirmed_at`, `worker_id`, `command`), `stream.ts` hub.
- **Identity components** (contract = Ladle story covering every state, both themes — `.ladle/components.tsx` renders every story in the dark and the light theme side by side —, and the mocked modifier):

| Component | Props (essential) | Story must show |
|---|---|---|
| `PhaseRail` | `segments` (from `phases.buildSegments(run, durations, confirmer)`), `variant: vertical \| horizontal \| mini`, `onSelect?`, `label?` (mini) | every run state, each failed segment, failed during apply, no changes, auto-confirmed, proposed, command, mocked |
| `StateBadge` | `state`, `outcome?`, `counts?`, `failedAt?`, `decision?: needsYou \| waiting`, `compact?` — `RunStateBadge` derives them from a `run`; modifiers are a separate `ModifierChips` | all states, all outcomes, needs you vs waiting, every modifier, stacked modifiers |
| `EnvCell` | `summary` (from `envSummary`), `needsYou?`, `selected?`, `downstream?: will_plan \| plans_if_outputs_change`, `title?` | each primary state, each modifier, empty env, PR active, selected, downstream-highlighted, 390 px |
| `RunActionBar` | `run`, `env`, `rights` (from `permissions.runRights`), `me`, `whoMayConfirm`, `triggerer?`, `confirmer?`, `cascade`, `destroyed`, `replaced`, `pending?`, `apiError?`, handlers, `prevDecision?` / `nextDecision?` | needs you, waiting on someone (each reason), four-eyes self-triggered, destroy without right, mocked, fallback, stale, finished applied, failed during apply, loading, API refusal |
| `PlanDiff` | `lines` (the plan log section, today) or `changes` (A1), `counts`, `query` + `onQueryChange`, `resourceAction?`, `emptyHint?` | create/update/delete/replace/read/moved, tag-only, long plan (500 resources), unparsed fallback, sensitive values, search |
| `LogViewer` | `chunks`, `live`, `failedHooks?`, `initialLine?`, `onLineLink?`, `lineAction?`, `renderAfterLine?` | live following, paused with new lines, sections with a failed hook, first error, search, timestamps, ANSI palette, 20 000 lines |
| `ProvenanceBadge` | `provenance` | every kind, mock, fallback, override |
| `TypeToConfirm` | `name`, `verb`, `because[]`, `counts`, `destroyed[]`, `replaced[]`, `cascade[]`, `pending?` | four-eyes apply, destroy, long destroy list, mobile sheet |

- **Graphs**: react-flow + dagre, loaded only on `/graph` (route-level code splitting and the `graph` chunk are kept from v1). Initial JS budget: ≤ 250 KB gz.
- **Tests**: vitest for `src/lib/*` (envSummary, runOutcome, phases, permissions, planParse, logModel, cascade, contrast ratios from `tokens.css`) and for identity components; Playwright happy path: today dev login → overview, at desktop width; the extension (attention zone lists a decision → run page → confirm with typed name → applied outcome, at 1440 and 390 px) is **[not built yet]**. `pnpm check:invariants` runs in CI with them. `pnpm lint`, `pnpm typecheck`, `pnpm test` must pass (DEV / CLAUDE §6).

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
