# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-05-08

First tagged release.

### Added
- **Workspaces tab.** Pulls workspace metadata and per-bucket token usage from
  the Anthropic Admin Usage Report API, computes USD cost per row from
  `pricing.json` (the same engine the rest of the dashboard uses for local
  JSONL data), and surfaces results alongside the existing tabs. Activity
  Anthropic returns with `workspace_id: null` (default workspace, Pro Max API
  access, keys not scoped to a workspace) appears as a synthetic
  `(Default / unattributed)` row instead of being silently dropped. Refresh
  is on-demand via a button; the entire sync runs inside one SQLite
  transaction so a partial Admin API failure cannot corrupt existing rows.
  Optional `ANTHROPIC_ADMIN_API_KEY` env var; the rest of the dashboard
  works unchanged without it.
- **Plan-aware subscription cost allocation.** `/api/projects` and
  `/api/sessions` responses now carry a `{rows, _meta}` shape. On
  subscription plans (pro/max/max-20x) each row displays its allocated
  share of the monthly fee weighted by the row's share of total API cost,
  rather than the raw API-equivalent number. The `api` plan still shows
  raw per-token cost.
- **Snapshot regression suite** (`tests/test_no_breakage.py`). Pins every
  pre-existing `/api/*` route's response shape to a JSON snapshot so
  additive features cannot quietly drift the existing tabs' contracts.

### Changed
- **WAL journal mode** is now enabled on the SQLite database. Without it,
  the default rollback journal serialized writers against readers and the
  scanner's initial-scan transaction blocked the dashboard's six parallel
  boot queries, manifesting as a hang on "loading…". WAL is a persistent
  per-database property; existing databases pick it up on next `init_db`.

### Fixed
- **Claude 4.x Opus pricing** corrected to current published rates: $5/MTok
  input, $25/MTok output (with caching multipliers giving $0.50 cache_read,
  $6.25 cache_create_5m, $10 cache_create_1h). The legacy $15/$75 rates
  were carried over from Claude 3 Opus and overstated 4.x cost ~3×. Sonnet
  4.6 and Haiku 4.5 entries were already correct.
  ([Source](https://docs.anthropic.com/en/docs/about-claude/pricing).)
- **Dashboard CLI startup.** The initial scan no longer blocks server
  startup — it runs in a daemon thread, with the existing 30-second
  `_scan_loop` picking up incremental work after. The browser open is
  deferred ~600ms via `threading.Timer` so it doesn't race the listener
  bind and land on `ECONNREFUSED`.

### Notes on cost reconciliation
- The dashboard computes cost from `tokens × pricing.json rates`, not from
  the Admin API `cost_report` endpoint. `cost_report.amount` is undocumented
  and in practice returns `workspace_id: null` for all rows, making
  per-workspace attribution impossible. Pricing-based math reconciles
  cleanly with both the Console per-workspace cost view and credit-balance
  consumption; `cost_report` was off by ~50× with no documentation
  explaining what its `amount` field represents.

[0.1.0]: https://github.com/nateherkai/token-dashboard/releases/tag/v0.1.0
