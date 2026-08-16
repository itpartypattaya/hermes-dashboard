# Changelog

## v0.3.0 — 2026-08-16

* **Favicon.** Pages and the settings page carry a browser-tab icon, inlined as a `data:`
  URI so both serving contexts show it without an extra request. Configurable through
  `agent.favicon` (and from the settings form); an empty value or a missing file renders
  no `<link>` rather than a broken one. The bundled default is the Hermes Agent mark
  (MIT, © 2025 Nous Research — `assets/NOTICE`).


## v0.2.1 — 2026-08-16

Fresh-install pass: cloned the repo next to an empty `HERMES_HOME`, applied the example
files verbatim and built.

* **Fixed: a missing `paths.venv_python` crashed the whole build** (`FileNotFoundError`
  from the quota collector) instead of skipping the collector. Both collectors are now
  fail-open with a line on stderr; covered by a test.
* **`budgets.env` is now read by the engine itself.** Previously only a wrapper that
  `source`d it made the file matter, and the settings-page help claimed otherwise. The
  build loads it into the environment (exported variables win); `CODEX_WEEKLY_INPUT_BUDGET`
  and `CODEX_SUB_USD_MO` fill a `0` in `dashboard.json`, `HERMES_DASHBOARD_QUOTA_ALERT_PCT`
  reaches the quota collector. Help texts and the example file say exactly that.
* The Cost view no longer claims "tariffs from budgets.env" — they come from `dashboard.json`.
* `examples/config-descriptions.en.json` — the format of the hand-written prose layer of
  the Config view, referenced from `dashboard.example.json`.
* README: per-feature prerequisites table (which parts need the agent's venv, the
  checkout, git, an admin key or a CSV), environment-variable reference, Linux-host note.
* Removed one private environment-variable name left in the Anthropic collector.


## v0.2.0 — 2026-08-16

**Settings became a real view of the dashboard.** Same stylesheet, sidebar, tabs and
theme as the pages; one field per setting instead of a JSON blob.

* Schema-driven form (`hermes_dashboard/settings_schema.py`): typed fields with hints
  that explain the *consequence* of a value, repeatable rows for paid providers, chats,
  watchdogs, cron categories and tool groups, and a save button per section.
* `budgets.env` is edited key by key, each key with its unit and meaning; saving rewrites
  values only — comments, their order and unknown keys survive.
* Billing-export upload, "rebuild now" and the build log live in their own section.
* Safety: only paths declared in the schema are written, numbers are validated, both
  files are backed up before replacement, CSRF plus same-host origin on every POST.

**The engine no longer ships anyone's settings as defaults.** No assumed provider, price,
weekly budget or character limit — `providers.paid` and `providers.free` start empty and
`{}` stays a valid config. Consequences are honest: with no declared character limit the
memory tile shows `—` instead of a percentage of an invented number, and with no market
reference tariff the "covered by subscription" estimate is omitted.

**Fixed a silent translation hole.** The string extractor read only the first fragment of
concatenated `_( "…" "…" )` texts, so long help texts were "translated" under a key that
never appears at runtime while the coverage check reported success. It now walks the AST;
five untranslated paragraphs surfaced and were translated.

**The hygiene test used to be the leak.** It listed the reference installation's literals
in the public repo. It now scans for *classes* of identifiers (chat ids, key literals,
absolute home paths, private-repo names) and takes your own strings from
`HERMES_DASHBOARD_SCAN_EXTRA`.

Also: human-readable labels for billing token types, `tools/make_demo.py` to build a
dashboard from invented telemetry without an agent, and a rewritten README.

Tests: 12 → 19.

## v0.1.0 — 2026-08-16

First public release, extracted from a private family-agent dashboard.

* Static pages built by cron from the agent's own telemetry: Overview, Cost, Memory,
  Automation, Config map, capability map.
* Everything agent-specific in `dashboard.json`; the engine is stdlib-only Python.
* English keys with a Russian locale; one static file per language plus a sidebar switch.
* Honest money: billing export or Cost API while it covers the window, otherwise the core
  estimate, otherwise tariff × tokens — always labelled.
* Sessions counted only when the model was actually called; forced fallback distinguished
  from deliberate paid runs.

---

**Note on git history.** The history was reset to a single commit at v0.2.1: the first
commits carried a test that listed the reference installation's own identifiers (host
alias, handles, private-repo name) as a hardcoded deny-list — the scanner was the leak it
was meant to prevent. It was replaced in the released code by a class-based scan that
takes your strings from `HERMES_DASHBOARD_SCAN_EXTRA`, and the old objects were dropped.
The versions above document the product's evolution; only v0.2.1 is tagged.

