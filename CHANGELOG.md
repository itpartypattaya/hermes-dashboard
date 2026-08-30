# Changelog

## v0.6.0 — 2026-08-30

A second sweep, over classes the first one did not cover. Four found, each with a
test that fails on v0.5.1 — and two of them were defects in the containment work
of v0.4.0 itself.

* **A bad byte stopped everything.** `UnicodeDecodeError` is a `ValueError`, so
  every `except OSError` around a `read_text(encoding="utf-8")` let it through.
  The files this reads are written by the agent, and a killed writer leaves a
  truncated multibyte sequence — one such byte in `memories/MEMORY.md` produced a
  build with **zero pages**, and a traceback in a log nobody reads. Agent-written
  files now go through `read_text_safe()`, which decodes leniently: a replacement
  character in one label beats a dashboard frozen on yesterday.
* **The build lock leaked on failure.** `_BuildLock` was a context manager driven
  by hand-written `__enter__`/`__exit__`, so any exception left the lock file
  behind and blocked every build for the next fifteen minutes. It is a `with`
  block now.
* **Containment covered five call sites out of thirty.** The v0.4.0 promise that a
  failing section costs only itself was true for `events`, `security`, `usage`,
  `cron` and `connectors` — and false for the KPI queries, the memory probe, the
  history CSV, the routing banner, every host probe and all four view builders.
  All of them are contained now; a failing probe renders as `—`.
* **The Python and SQL twins disagreed, but only on Linux.** `is_paid_row()` is the
  single-row twin of the `paid()` SQL. SQLite `LIKE` folds ASCII case; Python's
  `fnmatch` folds case only where the filesystem does — so the two agreed on the
  developer's Windows machine and disagreed on the Linux server, where a model
  name like `Gemini-3.0` would be paid in the cost card and unpaid in the routing
  banner, on the same page. The Python side now matches SQL semantics explicitly.
* Also: `validate()` covered two numeric settings out of nine; the other seven
  (subscription price, weekly budget, reference tariffs, cost freshness, memory
  limits) are checked now, and `Config.number()` makes the read path fall back
  instead of raising.

Tests: 48 → 54.


## v0.5.1 — 2026-08-30

A sweep prompted by the two outside PRs: both had found a place where an earlier
fix of mine had landed in one call site and not its siblings. So each class of bug
fixed in v0.4.0 and v0.5.0 was grepped across the whole repository. Five more
instances turned up, each now covered by a test that fails on v0.5.0.

* **Shared temp names** — `gen_config` still wrote its cached page through
  `<cache>.tmp`. The sixth writer; the other five were fixed in v0.4.0 and v0.5.0.
* **SQL literals from config** — the primary-provider card built
  `billing_provider='{pid}'` by hand while every other fragment goes through the
  quoting helper (now public as `sql_str`). An apostrophe in a provider id produced
  broken SQL and, since v0.4.0 contains section failures, a silently missing card.
* **Validated on save, not on read** — `tz()` still raised on an offset that
  `validate()` rejects, and `dashboard.json` is a file the README tells you to edit
  by hand. It falls back to UTC now; validation stays the loud path.
* **The activity rule** — per-job cron telemetry counted sessions that never called
  a model, and expressed the rule as a hand-typed copy of `active()` elsewhere.
* **Escaping in the redirect script** — the language map sat unescaped in the same
  expression as the value escaped in v0.4.0, and `i18n.languages` was checked for
  length rather than charset, so `a"b` and `../x` passed. A language code becomes
  both a JS object key and a filename; the charset check makes both safe by
  construction.

Also verified, and left alone because it was already correct: every string that
originates in the agent rather than in the engine — chat titles, job names,
`last_error`, model and source names, tool names, log lines, the git commit
subject — is escaped. A build with markup injected into all of them produces no
raw payload in either page.

Tests: 43 → 48.


## v0.5.0 — 2026-08-30

First outside contributions, from **@jedi108** (Vadim Tsurkov).

**Security (#1).** The settings POST handler took `lang` from the form body and put it
straight into `set_lang()` and into the `Location` header, so a decoded CRLF was HTTP
response-header injection. The v0.4.0 whitelist had only covered the GET/cookie path.
The language is now validated on both paths and the redirect is built with `urlencode`.
The same PR added a Content-Security-Policy and `X-Content-Type-Options`/`Referrer-Policy`
to settings responses, `hmac.compare_digest` for the CSRF check, `HttpOnly`/`Secure` on
the language cookie, `Content-Length` validation, and IPv6-correct same-host matching.

**Robustness (from #2, reworked).** The unique-temp-file fix that v0.4.0 applied to
`build.py` was missing from every other writer — `settings._atomic`, the CSV upload,
`history._write` and both collector caches all still derived the temp name from the
target. They share one `common.atomic_write()` now. Subprocesses we spawn no longer
inherit the agent's credentials: `child_environment()` strips anything credential-shaped
while keeping what a host needs to function (trust store, proxy, locale, and the Windows
variables without which a subprocess cannot start at all). Provider HTTP errors log the
parsed `error.message` instead of a raw network-controlled body.

Not adopted from #2: confining `out_dir` inside the Hermes home (a webroot never is —
it stops the build on every documented deployment) and a hardcoded `HOME`. Both are now
guarded by tests.

Tests: 32 → 43.


## v0.4.0 — 2026-08-16

A review round; every item below has a test that fails on the previous release.

**Security.** `?lang=` on the settings server was reflected into `<html lang>`, into hidden
form fields and into a `Set-Cookie` header without validation — reflected XSS against an
already-authenticated admin, which basic-auth does nothing to stop. The language is now
accepted only if it is one of `i18n.languages`, and it is escaped at the renderer as well
(attribute and JS-literal contexts).

**Correctness.**

* The Cost API was called with `limit=32`; daily buckets cap at **31**, so the request was a
  400 that the collector swallowed — leaving the card silently on an old CSV or an estimate.
  Legal page size, `has_more`/`next_page` followed, and the reason printed to stderr.
* `timezone.offset_hours: 24` passed validation and then crashed the build in
  `datetime.timezone()`; an uncompilable `usage.stt_log_regex` or `security.tirith_regex` did
  the same at `re.compile`. All three are rejected at save time now.
* Sessions where the model was never called could reach the primary card's session count, the
  per-source breakdown and the forced-fallback count — contradicting the first honesty rule in
  this README. All three require `api_call_count > 0` now. The "deliberate paid runs" KPI
  counted every non-forced source while calling them CLI runs; it is now an explicit
  `deliberate_paid()` condition with an honest label.

**Robustness.**

* Concurrent builds shared one temp file per page (`index.html.tmp`), so cron and a manual
  rebuild could fail to publish or publish a half-written page. Temp files are unique per
  write, a lock file in the output directory keeps a second build out, and the rename retries
  briefly (Windows refuses to replace a file a reader holds).
* A raising section used to abort the whole build. Each generator is wrapped now: the section
  is dropped, the reason goes to stderr, the rest of the page is published.
* The Automation view vanished entirely when every cron job was interval-based, taking the
  economics, billing tiers and per-job telemetry with it. Only the day axis is skipped now.
* The settings page wrote uploaded cost CSVs to a path derived from `HERMES_DASHBOARD_COST_CSV`
  while the collector read `HERMES_DASHBOARD_ANTHROPIC_COST_CSV`. One name, the documented one.

**Privacy.** The pages claimed to make no external calls while loading Google Fonts on every
view. The claim is corrected and `views.web_fonts: false` now removes the link entirely,
falling back to a system font stack — with the trade-off spelled out on the settings page.


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

