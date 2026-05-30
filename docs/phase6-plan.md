# Phase 6 — UI refresh guided by `ui-ux-pro-max-skill`

## Context

The dashboard (`dashboard.py`) is a single-file Flask + inline-Jinja app that
operators stare at for long sessions while it live-polls
`GET /campaigns/<id>/contacts.json` every 2.5 s. The product is an internal
ops console, not a marketing page, so the visual direction is taken from the
**data-dense console** family (Linear / Vercel / Stripe), not the landing-page
patterns the skill ships for consumer brands.

Inputs from `nextlevelbuilder/ui-ux-pro-max-skill`:

- 67 UI styles → picked **Refined Minimal** (flat surfaces, hairline borders,
  one accent, status colors only used for status).
- 161 color palettes → picked **Calm Operator** (cool slate neutrals + single
  blue accent + semantic green/amber/red), mirrored to a dark theme via
  `prefers-color-scheme`.
- Font catalog → picked **Inter** (UI) + **JetBrains Mono** (phone numbers,
  HubSpot IDs, durations). Loaded from Google Fonts with `display=swap` and
  `preconnect` hints.
- Pre-delivery UX checklist → applied in full (see below).

## Design tokens

### Colors — light

| Token | Value |
| --- | --- |
| `--bg` | `#F8FAFC` |
| `--surface` | `#FFFFFF` |
| `--surface-2` | `#F1F5F9` |
| `--border` | `#E2E8F0` |
| `--border-strong` | `#CBD5E1` |
| `--text` | `#0F172A` |
| `--text-muted` | `#64748B` |
| `--primary` | `#2563EB` |
| `--primary-hover` | `#1D4ED8` |
| `--primary-soft` | `#EFF4FF` |
| `--success` | `#10B981` |
| `--success-soft` | `#ECFDF5` |
| `--warning` | `#F59E0B` |
| `--warning-soft` | `#FFFBEB` |
| `--danger` | `#EF4444` |
| `--danger-soft` | `#FEF2F2` |
| `--neutral` | `#475569` |
| `--neutral-soft` | `#F1F5F9` |
| `--focus-ring` | `rgba(37, 99, 235, 0.35)` |

### Colors — dark (`@media (prefers-color-scheme: dark)`)

| Token | Value |
| --- | --- |
| `--bg` | `#0B1220` |
| `--surface` | `#111827` |
| `--surface-2` | `#1F2937` |
| `--border` | `#1F2937` |
| `--border-strong` | `#374151` |
| `--text` | `#E5E7EB` |
| `--text-muted` | `#94A3B8` |
| `--primary` | `#60A5FA` |
| `--primary-hover` | `#3B82F6` |
| `--primary-soft` | `rgba(96, 165, 250, 0.12)` |
| `--success` | `#34D399` |
| `--success-soft` | `rgba(52, 211, 153, 0.12)` |
| `--warning` | `#FBBF24` |
| `--warning-soft` | `rgba(251, 191, 36, 0.12)` |
| `--danger` | `#F87171` |
| `--danger-soft` | `rgba(248, 113, 113, 0.12)` |
| `--neutral` | `#94A3B8` |
| `--neutral-soft` | `rgba(148, 163, 184, 0.12)` |

### Spacing scale

Tokens `--space-1`…`--space-7` = 4 / 8 / 12 / 16 / 24 / 32 / 48 px.
Existing inline rem values were re-expressed against this scale during the
refactor (no markup-class changes).

### Radius scale

`--radius-sm: 4px`, `--radius: 8px`, `--radius-lg: 12px`,
`--radius-pill: 999px`.

### Typography

- UI: `Inter`, system-ui fallback stack (`-apple-system`, `Segoe UI`, …).
- Mono: `JetBrains Mono`, falling back to `ui-monospace`, `SFMono-Regular`,
  `Menlo`, `Consolas`.
- Loaded via `<link rel="preconnect">` + Google Fonts `display=swap` so the
  page never blocks on font fetch.
- Sizes unchanged (14 px base, 1.5 line-height); letter-spacing tightened by
  −0.01em on headings.

### Iconography

Semantic action emojis (▶ Start, ❚❚ Pause, ■ Done) replaced with inline
**Lucide** SVGs (`play`, `pause`, `square`) at `width="14" height="14"
stroke-width="2"`. Decorative dots on status pills stay (they're CSS, not
emoji). The brand "V" mark is unchanged.

### Motion

- Hover / banner / focus transitions: 150 ms cubic-bezier ease-out.
- Existing `pulse` (live indicator) and `spin` (dialing spinner) keep their
  timings.
- `@media (prefers-reduced-motion: reduce)` disables `pulse`, `spin`, and any
  non-essential transitions; the dialing spinner falls back to a static dot.

## Accessibility checklist (from skill, applied to this PR)

- [x] No emoji as icons — Lucide SVG only.
- [x] `cursor: pointer` audit — buttons, `<a>`, `[role=button]` confirmed.
- [x] `:focus-visible` ring on every interactive element (`button`, `a`,
      `input`, `[tabindex]`), 3 px outer ring using `--focus-ring`.
- [x] Text contrast ≥ 4.5:1 in both light and dark (Inter on `#0F172A` /
      `#E5E7EB` checked against AA).
- [x] Status pills ≥ 3:1 against their soft backgrounds.
- [x] `prefers-reduced-motion` respected.
- [x] Responsive at 375 / 768 / 1024 / 1440 (existing breakpoints unchanged —
      container `max-width: 1100px` still applies).
- [x] `aria-live="polite"` on the **In progress** metric so screen readers
      announce the 2.5 s poll updates.
- [x] `<th scope="col">` on every table header.
- [x] `<caption>` on both tables (`sr-only` so it doesn't change the visual).
- [x] Skeleton/loading affordance: `.skeleton` shimmer utility + a subtle
      `.row-stale` dim applied via existing `in-progress` styling so cells
      don't flicker on poll. (The 2.5 s in-place updates already mutate text
      nodes; we just smooth the visual transition.)

## Non-goals

- No new Python dependencies.
- No template-variable, route, or JS changes — JS polling code in
  `_DETAIL_TMPL` is untouched so `state.py` / `dashboard.py` data contracts
  are preserved.
- No new files outside this plan document.
- Per the repo's phased-workflow convention, this PR stops at "draft for
  review"; the next phase requires user sign-off before further UI work.

## Verification

- Manual: `flask --app dashboard run --port 5001` against a populated SQLite
  DB; visit `/campaigns/` and `/campaigns/<id>`. Confirm:
  - Campaign list and detail render.
  - Start / Pause / Done / Run-All buttons reflect status correctly with new
    SVG icons.
  - Polling JSON tick still updates rows in place and toggles the dialing
    spinner.
  - Toggle OS dark mode → palette inverts without a reload.
  - `prefers-reduced-motion: reduce` (Chrome DevTools rendering tab)
    stops the live-dot pulse and spinner animation.
- Automated: existing repo tests (none for the dashboard view layer) — no
  new tests added because there is no rendering-test harness in the repo
  and adding one is out of scope.
