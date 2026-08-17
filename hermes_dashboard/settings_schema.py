"""Declarative schema of the settings form: one field per metric.

The settings page is generated from this schema, and the POST handler applies
only what the schema declares — an unknown form field is ignored, so a stray
name in the browser can never inject a key into dashboard.json.

Field kinds
  text      — one line
  number    — float (money, tariffs)
  int       — whole number (tokens, hours, days)
  bool      — checkbox (a hidden "0" precedes it, so unchecking is a real value)
  select    — fixed options
  i18n      — one input per configured language → {"en": …, "ru": …}
  i18n_lines— one textarea per language, one item per line → {"en": [...], …}
  csv       — comma-separated line → list of strings
  map       — key/value rows → {"key": "value"}
  objects   — repeatable rows of sub-fields → [{...}, ...]

`help` explains the consequence of the value, not its name: the point of the
page is that someone can fill it in without reading the source.
"""
from __future__ import annotations

# ── sub-field sets for repeatable rows ──────────────────────────────────

PEOPLE_FIELDS = [
    {"key": "label", "kind": "text", "label": "Name", "primary": True, "width": "1fr"},
    {"key": "handle", "kind": "text", "label": "Handle", "width": "1fr"},
]

PAID_FIELDS = [
    {"key": "id", "kind": "text", "label": "billing_provider", "primary": True, "width": "1.1fr",
     "placeholder": "anthropic"},
    {"key": "label", "kind": "text", "label": "Shown as", "width": "1.2fr", "placeholder": "Claude Sonnet"},
    {"key": "in_per_m", "kind": "number", "label": "$/1M in", "width": ".7fr"},
    {"key": "out_per_m", "kind": "number", "label": "$/1M out", "width": ".7fr"},
    {"key": "model_like", "kind": "text", "label": "model LIKE", "width": ".9fr", "placeholder": "gemini-%"},
    {"key": "exclude_models", "kind": "csv", "label": "except models", "width": "1fr"},
    {"key": "cost_cache", "kind": "text", "label": "billing cache", "width": "1.1fr",
     "placeholder": "cache/<provider>_cost.json"},
    {"key": "rank", "kind": "int", "label": "rank", "width": ".5fr"},
]

FREE_FIELDS = [
    {"key": "id", "kind": "text", "label": "billing_provider", "primary": True, "width": "1fr"},
    {"key": "model", "kind": "text", "label": "model", "width": "1.2fr"},
    {"key": "label", "kind": "text", "label": "Shown as", "width": "1.2fr"},
]

WATCHDOG_FIELDS = [
    {"key": "id", "kind": "text", "label": "cron job id", "primary": True, "width": "1fr"},
    {"key": "label", "kind": "i18n", "label": "Tile title", "width": "1.4fr"},
    {"key": "schedule", "kind": "i18n", "label": "Schedule caption", "width": "1.2fr"},
]

CATEGORY_FIELDS = [
    {"key": "key", "kind": "text", "label": "id", "primary": True, "width": ".7fr"},
    {"key": "keywords", "kind": "csv", "label": "Keywords in the job name", "width": "1.6fr"},
    {"key": "class", "kind": "select", "label": "Colour", "width": ".8fr",
     "options": [("lime", "lime"), ("cy", "cyan"), ("am", "amber"), ("n", "neutral")]},
    {"key": "legend", "kind": "i18n", "label": "Legend", "width": "1.4fr"},
]

SYSJOB_FIELDS = [
    {"key": "match", "kind": "text", "label": "Substring of the crontab command", "primary": True, "width": "1.2fr"},
    {"key": "label", "kind": "i18n", "label": "Shown as", "width": "1.4fr"},
]

OAUTH_FIELDS = [
    {"key": "label", "kind": "text", "label": "Group", "primary": True, "width": "1fr"},
    {"key": "like", "kind": "text", "label": "tool_name LIKE", "width": "1.3fr"},
    {"key": "not_like", "kind": "text", "label": "and NOT LIKE", "width": "1fr"},
]

TOOLGROUP_FIELDS = [
    {"key": "prefix", "kind": "text", "label": "tool_name prefix", "primary": True, "width": "1.2fr"},
    {"key": "label", "kind": "text", "label": "Collapse into", "width": "1.2fr"},
]

# ── the form itself ─────────────────────────────────────────────────────
# section: {id, title, note, cards: [{title, intro?, fields: [...]}]}

SCHEMA = [
    {
        "id": "identity",
        "title": "Agent identity",
        "note": "how the dashboard introduces the agent",
        "cards": [
            {"title": "Name and header", "fields": [
                {"path": "agent.name", "kind": "text", "label": "Agent name",
                 "help": "Shown in the sidebar and in page titles."},
                {"path": "agent.glyph", "kind": "text", "label": "Glyph", "narrow": True,
                 "help": "One character or emoji in the sidebar badge."},
                {"path": "agent.host_label", "kind": "text", "label": "Host caption",
                 "help": "Small line under the name — platform and server, for your own orientation."},
                {"path": "agent.config_repo_label", "kind": "text", "label": "Footer line",
                 "help": "Printed in the page footer; usually where the config lives."},
                {"path": "views.web_fonts", "kind": "bool", "label": "Load web fonts from Google",
                 "help": "On: the designed typefaces, fetched from fonts.googleapis.com on every "
                         "view — the only external request a page makes, and it tells Google your "
                         "IP and when you opened the dashboard. Off: system fonts, nothing leaves "
                         "your server."},
                {"path": "agent.favicon", "kind": "text", "label": "Favicon file",
                 "help": "Browser-tab icon (.ico/.png/.svg), looked up in the agent home first, "
                         "then next to the engine. Inlined into every page, so keep it small; "
                         "empty means no icon."},
                {"path": "agent.tagline", "kind": "i18n", "label": "Kicker above the title"},
                {"path": "agent.description", "kind": "i18n", "label": "Overview intro", "textarea": True},
            ]},
            {"title": "People chips", "intro":
                "Optional chips under the Overview title — who this agent serves. Purely decorative: "
                "the numbers never come from here.",
             "fields": [
                 {"path": "agent.people", "kind": "objects", "label": "People", "item": PEOPLE_FIELDS,
                  "add_label": "Add person"},
             ]},
        ],
    },
    {
        "id": "runtime",
        "title": "Paths, language, timezone",
        "note": "where to read from and where to write",
        "cards": [
            {"title": "Paths", "fields": [
                {"path": "paths.hermes_home", "kind": "text", "label": "Agent home",
                 "help": "Where state.db, cron/jobs.json and config.yaml live. The HERMES_HOME env var wins over this."},
                {"path": "paths.out_dir", "kind": "text", "label": "Output directory",
                 "help": "Where the built pages are written. Must be served by your web server behind basic-auth."},
                {"path": "paths.venv_python", "kind": "text", "label": "Hermes venv python",
                 "help": "Needed by the Config view and the quota collector — they import the running core. "
                         "Empty means those two features stay off; everything else still builds."},
                {"path": "paths.node", "kind": "text", "label": "node binary (optional)",
                 "help": "If set, the Config view runs its inline JS through node as a self-check before publishing."},
                {"path": "paths.gateway_unit", "kind": "text", "label": "systemd unit of the gateway",
                 "help": "Checked with systemctl is-active for the Gateway tile."},
                {"path": "paths.settings_url", "kind": "text", "label": "Settings link on the dashboard",
                 "help": "Relative URL of this page; empty removes the link from the sidebar."},
            ]},
            {"title": "Language and time", "fields": [
                {"path": "i18n.default", "kind": "text", "label": "Default language", "narrow": True,
                 "help": "This language is written to index.html; the others to index.<lang>.html."},
                {"path": "i18n.languages", "kind": "csv", "label": "Languages to build",
                 "help": "Codes of shipped locales (en, ru). An unknown code falls back to English text."},
                {"path": "timezone.name", "kind": "text", "label": "Timezone name",
                 "help": "Printed on the cron axis; must match the agent's timezone."},
                {"path": "timezone.offset_hours", "kind": "int", "label": "UTC offset, hours", "narrow": True,
                 "help": "Used for event times and reset horizons. No DST handling — a whole-hour offset."},
                {"path": "timezone.label", "kind": "text", "label": "Short label", "narrow": True,
                 "help": "Suffix next to times, e.g. UTC or a city abbreviation."},
            ]},
        ],
    },
    {
        "id": "money",
        "title": "Providers and money",
        "note": "what counts as paid, at which tariff, and when a fallback is an incident",
        "cards": [
            {"title": "Primary provider", "intro":
                "The provider that answers by default. On a subscription its tokens are not billed per call, "
                "so its volume is shown against a reference budget rather than a bill.",
             "fields": [
                 {"path": "providers.primary.id", "kind": "text", "label": "billing_provider in state.db",
                  "help": "Exact value of sessions.billing_provider for the primary model."},
                 {"path": "providers.primary.label", "kind": "text", "label": "Shown as"},
                 {"path": "providers.primary.billing", "kind": "select", "label": "Billing type",
                  "options": [("subscription", "subscription — fixed price"), ("usage", "usage — per token")]},
                 {"path": "providers.primary.subscription_usd_month", "kind": "number", "label": "Subscription price",
                  "unit": "$/month", "help": "Fixed monthly cost. Added as-is to the 30-day total on the Cost view."},
                 {"path": "providers.primary.weekly_input_budget", "kind": "int", "label": "Weekly input reference",
                  "unit": "tokens", "help": "Only the scale for the progress bar (25000000 = 25M). "
                                            "Not a hard limit — 0 hides the bar."},
                 {"path": "providers.primary.ref_in_per_m", "kind": "number", "label": "Market reference, input",
                  "unit": "$/1M", "help": "Tariff of a comparable pay-per-token model. Used only to answer "
                                          "«what would this traffic have cost». 0 hides the estimate."},
                 {"path": "providers.primary.ref_out_per_m", "kind": "number", "label": "Market reference, output",
                  "unit": "$/1M"},
                 {"path": "providers.primary.quota_cache", "kind": "text", "label": "Quota cache file",
                  "help": "JSON written by the quota collector; empty means no live-quota card."},
             ]},
            {"title": "Paid providers", "intro":
                "Usage-billed providers — real money. One row per provider; the order is the fallback chain. "
                "«model LIKE» and «except models» narrow the match when one provider id serves both a paid and a "
                "free tier. «billing cache» is where an uploaded export lands: while its data covers the window, "
                "the card shows real $ instead of an estimate.",
             "fields": [
                 {"path": "providers.paid", "kind": "objects", "label": "Providers", "item": PAID_FIELDS,
                  "add_label": "Add provider"},
             ]},
            {"title": "Free tiers", "intro":
                "Free keys: never counted as money and never as an incident.",
             "fields": [
                 {"path": "providers.free", "kind": "objects", "label": "Free tiers", "item": FREE_FIELDS,
                  "add_label": "Add free tier"},
             ]},
            {"title": "What counts as a forced fallback", "fields": [
                {"path": "providers.fallback_sources", "kind": "csv", "label": "Interactive sources",
                 "help": "sessions.source values where the primary model answers by default (telegram, cron, line). "
                         "A paid session from these is an incident; a CLI run on a paid key is deliberate and is not."},
                {"path": "providers.cost_fresh_days", "kind": "int", "label": "Billing export is fresh for",
                 "unit": "days", "help": "While the export's newest day is within this many days, its numbers are used. "
                                         "Older than that, it drops out and the page falls back to an estimate."},
            ]},
        ],
    },
    {
        "id": "telemetry",
        "title": "Telemetry labels",
        "note": "how raw ids from the database are named on the page",
        "cards": [
            {"title": "Chats", "intro":
                "chat_id → readable name for the «Requests by chat» card. Unlisted chats fall back to the "
                "display_name stored in the database.",
             "fields": [
                 {"path": "chats.names", "kind": "map", "label": "Chats", "key_label": "chat_id",
                  "val_label": "Name", "add_label": "Add chat"},
             ]},
            {"title": "Session sources", "intro":
                "sessions.source → caption in the «Session sources» chart.",
             "fields": [
                 {"path": "sources", "kind": "map", "label": "Sources", "key_label": "source",
                  "val_label": "Caption", "add_label": "Add source"},
             ]},
            {"title": "Tool grouping", "intro":
                "OAuth groups are counted from messages.tool_name with SQL LIKE; tool groups collapse many "
                "technical names into one bar in «Top tools».",
             "fields": [
                 {"path": "usage.oauth_groups", "kind": "objects", "label": "OAuth connectors",
                  "item": OAUTH_FIELDS, "add_label": "Add group"},
                 {"path": "usage.tool_groups", "kind": "objects", "label": "Tool groups",
                  "item": TOOLGROUP_FIELDS, "add_label": "Add group"},
                 {"path": "usage.stt_log_regex", "kind": "text", "label": "STT log pattern",
                  "help": "Speech recognition lives outside tool telemetry, so it is counted by this regex in "
                          "agent.log — the page prints the real log window next to the number."},
             ]},
        ],
    },
    {
        "id": "automation",
        "title": "Automation and security",
        "note": "cron, watchdogs, the rules file",
        "cards": [
            {"title": "Cron jobs", "fields": [
                {"path": "cron.dream_job_id", "kind": "text", "label": "Nightly consolidation job id",
                 "help": "Its successful run is the one green line in the event feed. Empty means no such line."},
                {"path": "cron.name_strip_prefixes", "kind": "csv", "label": "Owner prefixes to strip",
                 "help": "Cut from the beginning of job names so the timeline is readable."},
                {"path": "cron.watchdogs", "kind": "objects", "label": "Watchdog tiles", "item": WATCHDOG_FIELDS,
                 "add_label": "Add watchdog"},
                {"path": "cron.categories", "kind": "objects", "label": "Timeline categories",
                 "item": CATEGORY_FIELDS, "add_label": "Add category"},
                {"path": "cron.system_jobs", "kind": "objects", "label": "System crontab lines to plot",
                 "item": SYSJOB_FIELDS, "add_label": "Add line"},
            ]},
            {"title": "Security section", "intro":
                "Only measurable things get a tile. Without a rules file and an evals file the whole section "
                "disappears instead of showing zeros.",
             "fields": [
                 {"path": "security.shield_file", "kind": "text", "label": "Rules file",
                  "help": "Its last git change date becomes the card's badge."},
                 {"path": "security.shield_bullets", "kind": "i18n_lines", "label": "Rules card bullets",
                  "help": "One bullet per line. Simple HTML like <b> is allowed."},
                 {"path": "security.evals_path", "kind": "text", "label": "Evals file",
                  "help": "JSON with an «evals» list; its length becomes the scenario count."},
                 {"path": "security.evals_bullets", "kind": "i18n_lines", "label": "Evals card bullets"},
                 {"path": "security.alert_channel_label", "kind": "text", "label": "Where alerts go",
                  "help": "Mentioned in the tile when the command scanner fails open."},
             ]},
            {"title": "Memory", "fields": [
                {"path": "memory.memory_file", "kind": "text", "label": "Bounded rules file"},
                {"path": "memory.user_file", "kind": "text", "label": "Bounded profile file"},
                {"path": "memory.backup_branch", "kind": "text", "label": "Backup branch",
                 "help": "Mentioned in the Memory view caption; empty hides the mention."},
                {"path": "memory.memory_char_limit_default", "kind": "int", "label": "Rules limit fallback",
                 "unit": "chars", "help": "Used only when config.yaml declares no limit. 0 = show «—» instead of a "
                                          "made-up percentage."},
                {"path": "memory.user_char_limit_default", "kind": "int", "label": "Profile limit fallback",
                 "unit": "chars"},
            ]},
            {"title": "Views and the capability map", "fields": [
                {"path": "views.config_map", "kind": "bool", "label": "Build the Config view",
                 "help": "Needs the Hermes venv python. Off removes the tab entirely."},
                {"path": "views.connectors", "kind": "bool", "label": "Build the capability map"},
                {"path": "connectors.skills_from_gitignore", "kind": "bool", "label": "«Our» skills from .gitignore",
                 "help": "On: custom skills are the allowlist entries !/skills/<name>/. Off: every directory in skills/."},
                {"path": "config_map.truth_ref", "kind": "text", "label": "Git ref of the source of truth",
                 "help": "The Config view compares the live config against this ref and marks the difference as drift."},
            ]},
        ],
    },
]

# ── budgets.env: help for known keys ────────────────────────────────────
# The file itself is free-form KEY=value; every key found gets its own field,
# and these are the ones the engine (and the usual alert cron) actually read.

BUDGET_HELP = {
    "CODEX_WEEKLY_INPUT_BUDGET": ("tokens", "Weekly input reference for your alert cron; the Cost view bar "
                                            "uses it only while providers.primary.weekly_input_budget is 0."),
    "CODEX_SUB_USD_MO": ("$/month", "Subscription price for your alert cron; the 30-day total uses it "
                                    "only while providers.primary.subscription_usd_month is 0."),
    "HERMES_DASHBOARD_QUOTA_ALERT_PCT": ("%", "The quota alert speaks up when less than this share of the "
                                              "subscription window is left."),
}
