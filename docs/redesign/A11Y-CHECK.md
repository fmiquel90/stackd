# Keyboard and screen-reader check — run page and matrix

Stage 4 definition of done: "keyboard and screen-reader check on the run page and the matrix".
Run on the seeded demo data, Chromium 1440×900, as `alice` (approver, tiers dev/staging/prod).

**How it was checked.** A Playwright script drove the keyboard only (Tab, the documented
shortcuts, arrows, Esc, Enter) and recorded, for each focus stop, the element, its label and its
focus outline; the accessible names of the action buttons were then checked separately with
role-and-exact-name queries. The accessible tree (roles, names, live regions) was read
from the same session. No desktop screen reader (NVDA, VoiceOver) is available in this
environment: the screen-reader part is a check of what assistive technology receives — names,
roles, states and announcements — not a listening session. A pass with a real screen reader is
still worth doing before release.

## Run page (`/runs/:id`, an unconfirmed prod plan)

Tab order, first 26 stops — every stop shows a 2 px solid focus outline (`--color-focus`):

| # | Element | Accessible name |
|---|---|---|
| 1 | link | Go to content (skip link) |
| 2–8 | links | StackD home, Overview "3 decisions need you", Graph, Queue, Workers "1/1 workers online", Variable sets, Settings |
| 9–10 | buttons | space switcher, user menu |
| 11–13 | links | breadcrumb: demo › demo-network › prod |
| 14–15 | buttons | Open the command palette; Notifications (unread count) |
| 16–17 | link, button | demo-network / prod; Copy commit SHA |
| 18–20 | buttons | Cancel run, Discard, Confirm apply — exact accessible names, each with `aria-keyshortcuts` (x, d, c); the visible key hint is `aria-hidden` |
| 21–22 | link, button | Next run that needs you; Re-plan on head |
| 23–24 | buttons | phase rail segments, named with status and duration ("Plan: done, <1s · +4 ~0 −0 — open logs") |
| 25–26 | tab, tabpanel | "Plan +4 ~0 −0" and its panel |

Shortcuts and focus management:

- `c` opens the confirmation popover with focus in the type-the-name field (prod is a four-eyes
  tier); `Esc` closes it and returns focus to "Confirm apply".
- Single-key shortcuts do nothing while typing or while a dialog/popover is open, and can be turned
  off in Settings (WCAG 2.1.4).
- Two polite live regions on the page: the app announcer (`role="status"`), which reads state
  transitions ("Run is now applying."), and the action bar's state sentence. API refusals render
  as `role="alert"` banners.

## Overview matrix (`/s/demo`)

The matrix is an ARIA grid; one cell is in the tab order (roving tabindex), the arrows and
`h/j/k/l` move between cells, and the selection is mirrored in the URL (`?sel=stack/env`):

| Key | Focused cell | Accessible name (truncated) |
|---|---|---|
| Tab (24th stop) | 0:0 | demo-app / dev, applied, 1h ago |
| → | 0:1 | demo-app / staging, applied, 1h ago, drifted |
| l | 0:2 | demo-app / prod, unconfirmed, apply blocked, plan +3 ~0 −0, 1h ago, mocked |
| j | 1:2 | demo-dns has no prod environment |
| ↓ | 2:2 | demo-network / prod, unconfirmed, needs your decision, plan +4 ~0 −0, … |
| h | 2:1 | demo-network / staging, unconfirmed, needs your decision, plan +1 ~1 −1, … |
| k | 1:1 | demo-dns / staging, unconfirmed, waiting on someone, plan +0 ~0 −3, … |
| s | — | marks the downstream cascade (1 env marked, announced) |
| Enter | — | opens `/s/demo/stacks/demo-dns/envs/staging` |

Each cell's name carries the state in words, the decision form (needs your decision / waiting on
someone / apply blocked), the plan counts and the modifiers (drifted, mocked…), so nothing relies on
the colour. Every cell shows the focus outline.

## Defects found by the check and fixed before the screens were committed

- Matrix cells had `outline-none`: no visible focus. Fixed (focus ring on the cell).
- Matrix cell names ran their parts together ("demo-app / devapplied1h ago"). Fixed with a composed
  `aria-label` (`cellLabel`, unit-tested).
- Fast arrow presses could move from a stale position. Fixed: the position is read from the focused
  cell, not from React state.
- Keyboard hints inside buttons were read as part of the name ("Confirm applyc"). Fixed with
  `aria-hidden` on the hint and `aria-keyshortcuts` on the button.
