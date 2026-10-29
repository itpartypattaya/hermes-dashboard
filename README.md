# hermes-dashboard

A **static system dashboard** for a [Hermes Agent](https://github.com/NousResearch/hermes-agent)
installation: live host status, security watchdogs, model routing, API usage with
**honest cost**, memory health, cron economics and a capability map — rendered as
self-contained HTML pages by a cron job, in as many languages as you configure.

Everything comes from the agent's own telemetry: `state.db`, `memory_store.db`,
`cron/jobs.json`, `config.yaml`, systemd, git. **Nothing is typed in by hand — if a
number cannot be collected automatically, it is not on the page.**

`MIT` · `Python ≥ 3.9` · stdlib only · no build step, no runtime on the page, no network calls except the web fonts (`views.web_fonts: false` removes those too)

---

## See it before you install it

```bash
git clone https://github.com/itpartypattaya/hermes-dashboard
cd hermes-dashboard
python tools/make_demo.py /tmp/demo      # invents an agent: 30 days of fake telemetry
xdg-open /tmp/demo/index.html            # or: open /tmp/demo/index.html
```

`make_demo.py` builds a throwaway `HERMES_HOME` (sessions, chats, cron jobs, memory
files, a billing export) and runs the normal build against it, so the demo goes through
exactly the same code as production. Nothing in it is real.

## What you get

| View | Content |
|---|---|
| **Overview** | gateway / uptime / load / disk / RAM, sessions and fallback KPIs with week-over-week deltas, the routing chain with the *live* active step, a curated event feed, security tiles (scanner fail-open, watchdog status, rules-file date, evals count) |
| **Cost** | primary-model card (weekly budget bar, cache hit rate, reasoning share, session sources), live subscription quota, one card per **paid** provider with money in three layers of certainty, free tiers, OAuth connectors, auxiliary providers — then analytics: economics, routing health, tokens by day (stacked), activity, top tools, requests by chat |
| **Memory** | durable facts and their growth, bounded files against their limits, memory-tool calls, transcript count, the memory pipeline diagram |
| **Memory map** *(experimental)* | *off by default* (`views.memory_map`); see [Memory map (experimental)](#memory-map-experimental) for its risks: what the agent actually remembers, from the holographic plugin's read-only export — overview with what waits for your decision, people and topics, a card per name and per fact (source, trust in words, versions), contradictions, weak basis, the week, removed and closed (a forgotten fact is only its number and date), about the snapshot |
| **Automation** | day timeline of every cron job (agent cron + system crontab), no-op economy, jobs grouped by billing tier, per-job token telemetry |
| **Config** | live config map: engine defaults ↔ effective `config.yaml` ↔ a git truth ref, with a **drift** column and hand-written descriptions |
| **Capability map** | separate page: MCP connectors, channels, plugins, model chain, web access, custom skills, bundled packs — read from the real config |
| **Settings** | its own page in the same design system: every setting as a field, the budget file key by key, billing-CSV upload, rebuild now, build log |

Dark/light theme with a toggle, print-friendly (`@media print` unfolds every view),
mobile tabs, Google Fonts (Unbounded / Onest / JetBrains Mono). The browser-tab icon
defaults to the Hermes Agent mark bundled in `assets/favicon.ico` (MIT, © Nous Research —
see `assets/NOTICE`); point `agent.favicon` at your own file, or set it to `""` for none.
It is inlined as a `data:` URI so the static pages and the settings server — served from
different paths — show the same icon without an extra request.

## Honesty rules baked in

These are the opinions of this dashboard — they are what its numbers mean.

* **A session is a session where the model was called.** Group chats create a session
  per observed thread with zero model calls; those are reported separately as
  "no answer", never as activity. (On a real install that was a quarter of the count.)
* **Paid vs free is decided by `billing_provider` *and* a positive model match.** One
  provider id can serve both a paid slot and a free key, and older cron fallbacks kept
  the primary model name under a foreign provider — a negative rule ("anything but the
  free model") invents money that was never spent.
* **A forced fallback is a paid session from an interactive source** (`telegram`,
  `cron`, `line` by default). A CLI run on a paid key is deliberate work, not an incident.
* **Money is labelled by the layer it came from:** real $ from a billing export or a
  Cost API *only while its data still covers the window*, otherwise the core estimate
  (`sessions.estimated_cost_usd`), otherwise tariff × tokens. A stale export is never
  passed off as today, and missing data is never printed as "$0.00 spent".
* **Log-derived metrics print the real window of the log**, not a round "30 days".
* **No detector, no metric.** Something nobody measures is absent, not an eternal zero.
* **No manual readings.** Balances typed in by hand rot on the page without anyone
  noticing, so the engine gives them nowhere to live.

## Install

```bash
git clone https://github.com/itpartypattaya/hermes-dashboard ~/.hermes/hermes-dashboard
mkdir -p ~/.hermes/dashboard
cp ~/.hermes/hermes-dashboard/dashboard.example.json ~/.hermes/dashboard/dashboard.json
cp ~/.hermes/hermes-dashboard/budgets.example.env    ~/.hermes/dashboard/budgets.env
$EDITOR ~/.hermes/dashboard/dashboard.json      # name, timezone, providers, chats, out_dir …
~/.hermes/hermes-dashboard/bin/hermes-dashboard build --config ~/.hermes/dashboard/dashboard.json
```

Then wire it up: `examples/crontab` for regeneration, `examples/nginx.conf` for
basic-auth over the output directory (and the settings page), `examples/systemd/` if you
want the settings server running as a service.

**Requirements.** A Linux host (host tiles read systemd, `/proc` and `crontab -l`; on
anything else the pages still build and those tiles show `—`) and a stdlib-only
Python ≥ 3.9 (syntax-checked for 3.9, run daily on 3.11). Nothing to `pip install`.

**Optional features and what each one needs** — everything below fails open: if the
prerequisite is missing, the section is skipped or reads its previous cache, the rest of
the page builds, and stderr says why.

| Feature | Needs |
|---|---|
| **Config** view (engine defaults ↔ effective ↔ truth ref) | `paths.venv_python` = the agent's venv (imports the core's `DEFAULT_CONFIG` and PyYAML), the agent checkout at `$HERMES_HOME/hermes-agent`, and `git` for `config_map.truth_ref` (drift against `origin/main` needs a remote). `paths.node` is optional and only runs a self-check of the view's inline script. |
| **Subscription quota** card | `paths.venv_python` again (the collector calls the core's `account_usage` API for `providers.primary.id`) and `providers.primary.quota_cache` set. |
| **Real $ on a paid-provider card** | an Anthropic admin key in `HERMES_DASHBOARD_ANTHROPIC_ADMIN_KEY` / `ANTHROPIC_ADMIN_KEY` (env or `.env`), **or** a Cost Report CSV in `cache/` (upload it from the settings page). Other providers stay on the estimate. |
| **Memory map** tab | the holographic memory plugin with its `memory_map.py` exporter at `memory_map.plugin_dir`, and `views.memory_map: true`. The exporter runs in its own process with a timeout; the plugin decides what may be shown, the tab only draws it. A failed export leaves the tab saying so, never numbers. |
| **Config descriptions** (the prose column of the Config view) | a hand-written JSON per language at `paths.config_descriptions.<lang>` — format in `examples/config-descriptions.en.json`. |
| **Week-over-week deltas** | nothing, but they need history: the build appends one row per day to `paths.history_csv`, so deltas appear after the first week. |

## Where the numbers come from

| On the page | Source |
|---|---|
| tokens, cost, sessions, providers, chats | `state.db` → `sessions` |
| tool calls, memory-tool usage, OAuth connectors | `state.db` → `messages.tool_name` |
| durable fact count | `memory_store.db` → `facts` |
| memory map (when on) | the holographic plugin's `memory_map.py --json` over one store (+ the nightly review's files about that store) |
| bounded memory fill | `memories/*.md` against the limits in `config.yaml` |
| cron timeline, job status, watchdogs, no-op economy | `cron/jobs.json` + `crontab -l` |
| model chain, vision/STT/TTS, MCP servers, plugins, channels | `config.yaml` (+ `.env` **variable names** only — values are never read) |
| gateway state, uptime, load, disk, RAM | systemd and `/proc` |
| last sync and deploy | the autosync log and `git log` |
| real $ | a billing export CSV or a provider Cost API, cached under `cache/` |
| week-over-week deltas | a CSV the dashboard appends to itself, one row per day |

## Configuration — `dashboard.json`

The engine assumes nothing about your agent: no provider, price, chat or limit is
hardcoded. A missing key falls back to a neutral default, so `{}` is a valid config (the
pages render, just unlabelled). See `dashboard.example.json` — or edit everything from
the settings page.

| Group | What it controls |
|---|---|
| `agent` | name, glyph, host caption, footer line, favicon, tagline/description per language, people chips |
| `paths` | `hermes_home`, `out_dir`, `venv_python`, `node`, `budgets_env`, `config_descriptions` per language, `settings_url`, `gateway_unit` |
| `i18n` | default language and the list to build |
| `timezone` | name, whole-hour UTC offset, short label |
| `providers` | `primary` (billing type, subscription price, weekly reference budget, market reference tariff, quota cache), `paid[]` (id, label, tariffs, `model_like`/`exclude_models`, `cost_cache`, rank), `free[]`, `fallback_sources`, `cost_fresh_days` |
| `chats`, `sources` | chat_id → name, `sessions.source` → caption |
| `usage` | OAuth groups (SQL LIKE), tool-name grouping, the STT log pattern |
| `cron` | nightly-consolidation job id, watchdog tiles, owner prefixes to strip, timeline categories, system crontab lines to plot |
| `security` | rules file, evals file, bullets per language, scanner regex, alert channel label |
| `memory` | bounded file paths, limit fallbacks, backup branch label |
| `connectors` | per-key descriptions and curated extra cards for the capability map |
| `views` | whether to build the config map and the capability map at all, `memory_map` (default off) for the memory map tab; `web_fonts` (default on) toggles the Google Fonts link |
| `memory_map` | `plugin_dir` (the folder with `memory_map.py`), `store`, `python`, `timeout_s`, `dreaming` (read the nightly review's questions about this store) |

## Settings page

```bash
hermes-dashboard settings --config ~/.hermes/dashboard/dashboard.json --port 8648
```

Binds to **127.0.0.1** and expects to sit behind the same basic-auth as the pages. It
renders as a regular dashboard view — same stylesheet, sidebar, tabs and theme — and is
organised as:

* **Data and rebuild** — upload a billing export CSV (it lands where the paid-provider
  card reads it), rebuild now, read the build log.
* **Budget file** — `budgets.env` shown key by key, each with its unit and what it
  actually changes. It is a plain shell file of `KEY=value` lines; the build loads it
  into the environment (a variable already exported wins), so the same numbers reach the
  dashboard and any alert cron of yours that `source`s the file. The values are
  **reference points, not limits** — nothing there throttles the agent. Precedence:
  `dashboard.json` is the source for the weekly bar and the subscription price;
  `CODEX_WEEKLY_INPUT_BUDGET` / `CODEX_SUB_USD_MO` fill in only while the config number
  is `0`; `HERMES_DASHBOARD_QUOTA_ALERT_PCT` is read by the quota collector's `--alert`
  mode. Saving rewrites only the values: comments, their order and unknown keys survive,
  and a new variable can be appended from the form.
* **Five configuration sections** — identity; paths, language and timezone; providers and
  money; telemetry labels; automation and security. Every setting is its own field with a
  hint that explains the *consequence* of the value ("0 hides the bar", "a paid session
  from these sources counts as an incident"). Repeatable things — paid providers, chats,
  watchdogs, cron categories, tool groups — are rows you add and remove. Each section
  saves on its own.
* **Raw file** — the whole `dashboard.json`, for anything the form does not cover.

Safety: every POST needs the CSRF token minted at start-up and a same-host Origin;
**only paths declared in the form schema are written**, so an unknown field name cannot
inject a key; numbers are validated (a typo is rejected, not silently stored as `0`);
`dashboard.json` and `budgets.env` are backed up before they are replaced; it never runs
arbitrary shell — "rebuild" spawns one fixed command; uploads are size-capped and must
look like a cost export.

## Environment variables

| Variable | Effect |
|---|---|
| `HERMES_HOME` | the agent home; overrides `paths.hermes_home` |
| `HERMES_DASHBOARD_CONFIG` | path of `dashboard.json` when `--config` is not given (then `$HERMES_HOME/dashboard/dashboard.json`) |
| `HERMES_DASHBOARD_PYTHON` | interpreter used by `bin/hermes-dashboard` (default `python3`) |
| `HERMES_DASHBOARD_TRUTH_REF` | overrides `config_map.truth_ref` for one build |
| `HERMES_DASHBOARD_ANTHROPIC_ADMIN_KEY` / `ANTHROPIC_ADMIN_KEY` | Cost API access for real $ |
| `HERMES_DASHBOARD_ANTHROPIC_COST_CSV` | explicit path of a Cost Report CSV instead of the newest one in `cache/` |
| `HERMES_DASHBOARD_QUOTA_ALERT_PCT` | threshold for the quota collector's `--alert` mode (also settable in `budgets.env`) |
| `MODEL_MAIN` | name of the primary model when `config.yaml` does not declare `model.default` |
| `HERMES_DASHBOARD_SCAN_EXTRA` | your own strings for the hygiene test |

## Languages

English strings are the keys; `hermes_dashboard/locales/ru.json` maps them and a missing
entry falls back to English. Each language is a separate static file (`index.html`,
`index.ru.html`, and the same for the capability map); the switch in the sidebar
remembers the choice. To add a language: drop `locales/<code>.json` next to `ru.json`,
list the code in `i18n.languages`, and fill in whatever
`python tools/extract_strings.py <code>` prints as missing — the test suite fails while a
locale is incomplete.

## Deployment notes

* Regenerate from cron (`examples/crontab`). A build takes seconds and writes atomically,
  so a reader never catches half a page.
* The pages are private by design: `noindex`, no page runtime, no secrets — only aggregates,
  **except the memory map tab** when it is on: it shows the texts of the agent's facts about
  people. Put the pages behind basic-auth over TLS (`examples/nginx.conf`) — with the memory
  map on, that is a requirement, not a nicety.
* **One external request remains by default:** the typefaces come from `fonts.googleapis.com`,
  so every view tells Google an IP and a timestamp. For a dashboard nobody should know you are
  reading, set `views.web_fonts: false` — the layout falls back to the system stack and the page
  then reaches nothing but your own server.
* Keep the generated HTML out of git; keep `dashboard.json`, `budgets.env` and your
  config-description files in it.
* stderr matters: a section that raises is caught, logged as `[section] <name> failed: …`
  and left out of the page — the rest still builds, because a dashboard missing one card
  beats no dashboard at all. Collectors behave the same way (`[collector] …`) and keep the
  previous cache. None of this changes the exit code, so send the cron output somewhere you
  occasionally read.
* Two builds at once (cron firing while you press "rebuild") are serialised by a lock file
  in the output directory; the loser logs and exits. Writes are atomic per file regardless.

## Tests

```bash
bin/hermes-dashboard check
```

Runs against a synthetic `HERMES_HOME`: config validation, SQL classification (including
that a `{}` config never guesses traffic is paid), i18n coverage, a full build on an
empty home and on a fake `state.db` (div balance, no non-English text on the English
page), the settings-form round-trip (unknown fields ignored, empty rows dropped, comments
preserved in the budget file, the page rendering inside the design system), and a hygiene
scan for classes of leaked identifiers. To check your own installation's strings too:

```bash
HERMES_DASHBOARD_SCAN_EXTRA="my-host,my-repo,@myhandle" bin/hermes-dashboard check
```

## Memory map (experimental)

The **Memory map** tab is new and experimental: what it shows, its wording and the
export format behind it may change between releases. It is off unless
`views.memory_map` is `true`. Read the risks below first.

### Turning it on

1. Install the holographic memory plugin 0.24.0 or later (it has `memory_map.py`) in the agent:
   `hermes plugins install holographic`. The dashboard does not copy or import it; it runs the plugin's exporter.
2. In `dashboard.json` set the view and say where the plugin and the store are (paths relative to the agent home):

   ```json
   "views": { "memory_map": true },
   "memory_map": {
     "plugin_dir": "plugins/holographic",
     "store": "memory_store.db",
     "python": "",
     "timeout_s": 60,
     "dreaming": true
   }
   ```

   `store` is one file: the profile's `memory_store.db`, or one scope's `.db` for a map of one user or chat (scopes
   are never mixed). `python` empty uses `paths.venv_python`. `dreaming` adds what the nightly review
   ([hermes-dreaming](https://github.com/itpartypattaya/hermes-dreaming)) asks about this store.
3. Build once by hand (`bin/hermes-dashboard build --config <your dashboard.json>`) and open the **Memory map** tab.
   "Memory map unavailable" names the reason (no plugin, no store, an exporter without `--config`, a timeout); the
   build log has `[section] memory-map failed: …`.
4. Check the page is behind the password: a request without credentials must get `401`
   (`curl -s -o /dev/null -w "%{http_code}" https://your-host/`). The cron entry of `examples/crontab` rebuilds it with
   the other pages.

To try the tab without an agent, `python tools/make_demo.py /tmp/demo` builds the demo site (an invented family) with it on.

### Before you turn it on

* **It shows personal data.** Unlike the rest of the dashboard (aggregates), the tab
  holds the texts of what the agent remembers — names, family, plans, habits — in the
  generated HTML. Serve it **only behind basic-auth over TLS** (`examples/nginx.conf`);
  never on a public URL, never in a repository, never forwarded. A copy someone saved or
  forwarded is not reached by a later «forget»: deleting it is up to them.
* **Every local account on the server can read it unless you close the folder.** Basic-auth
  guards the web only. Pages are written readable by all (0644), so on a shared host (a
  hosting panel, other sites' PHP) any local user reaches them when every folder from `/`
  to `out_dir` lets others in. With the tab on, the build log says so
  (`[section] memory-map: every local user can read the pages…`). Close the folder to all
  but the web server's group, keeping the group for new files:
  `sudo chgrp www-data <out_dir> && sudo chmod 2750 <out_dir>` (the user that builds keeps
  writing as owner; use your web server's group if it is not `www-data`).
* **The exporter gets no credentials.** It runs with the agent home and the dashboard's
  environment minus anything shaped like a secret, and minus the Anthropic admin key the
  usage collector reads. A failure line in the build log names the error's kind, never its
  message (a message could quote a stored text). On Linux a timeout stops the exporter's
  whole process group, so a wrapper in `memory_map.python` does not leave it running.
* **It is a snapshot from the last build.** The tab is regenerated with the rest of the
  pages by cron (`examples/crontab`: every 30 minutes), so it can be up to that old: a
  fact forgotten a minute ago can still show its text until the next build.
* **It needs a holographic plugin with `memory_map.py` and its `--config` option** —
  holographic 0.24.0 or later. The dashboard passes `--config
  <home>/config.yaml` whenever that file exists, and an exporter without the option
  exits with an error: the tab then says "Memory map unavailable" and the build log
  gets `[section] memory-map failed`.
* **One store, read-only.** The exporter opens one store read-only and applies every
  privacy rule itself (a forgotten fact is its number and date, a corrected version
  and what the safety screen hides carry no text); the tab only draws what it gets.
  The nightly review's files are read only about that store. Records of those files in
  a shape the exporter does not know are left out and counted on "About the snapshot".
* **Its readings are narrow.** "Person" or "topic" comes from a short closed list of
  words beside a name (most names stay "not determined"); "which fact the agent leans
  on" holds only for a question that fits both facts equally and says nothing with the
  reranker on; contradictions are the plugin's heuristic. There is no usage journal yet.
  The plugin's `docs/memory-map.md` lists all of its limits.
* **The whole map is inside `index.html`.** Every visit to any tab downloads it: about
  50 KB of export for 40 facts, but on a test store of 10,000 facts and 1,000 names the
  page grew to about 26 MB and took 8 s to draw per language. There is no cap yet.
* **A build waits for the export** (`memory_map.timeout_s`, 60 s by default). On a
  store in rollback-journal mode the agent's writes wait while it reads (under a second
  on a 3,000-fact test store).

## Limitations

* Reads a Hermes Agent installation's SQLite state — it is not a generic LLM-usage tool.
  Column names follow the core's `state.db` schema; a core release that renames them
  shows up as a section reading `unavailable`, not as wrong numbers.
* Real-$ collector and quota collector exist for one provider each (Anthropic Cost
  API/CSV; the core's `account_usage` for the subscription). Others: estimate only.
* `timezone.offset_hours` is a whole-hour offset; no DST transitions.
* The Config view and the quota collector need the agent's venv python; without it they
  are skipped.
* Real money needs an export or a Cost API from your provider. A provider that publishes
  neither stays on an estimate — and the page says so.

## Origin

Extracted from the private dashboard of a family Hermes agent; that private repo now
consumes this engine and keeps only its own `dashboard.json`. MIT.
