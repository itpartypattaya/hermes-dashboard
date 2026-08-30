"""dashboard.json — the single place for everything agent-specific.

The engine never hardcodes an agent name, a chat id, a provider price or a
cron job id. All of that comes from a JSON file (see dashboard.example.json).
Unknown keys are ignored, missing keys fall back to defaults, so a minimal
config is `{}` — the dashboard then still renders, just with generic labels
and without the curated extras.

Loading order: `HERMES_DASHBOARD_CONFIG` env → `--config` argument →
`$HERMES_HOME/dashboard/dashboard.json` → defaults only.
"""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any

DEFAULTS: dict[str, Any] = {
    "agent": {
        "name": "Hermes",
        "glyph": "◆",
        "host_label": "hermes agent",
        "tagline": {"en": "System dashboard · Hermes Agent", "ru": "Системный дашборд · Hermes Agent"},
        "description": {
            "en": "Live server status, security, models and their usage, memory and automation.",
            "ru": "Живой статус сервера, безопасность, модели и их использование, память и автоматика.",
        },
        "people": [],                     # [{"label": "Alice", "handle": "@alice"}]
        "favicon": "assets/favicon.ico",   # data: URI in every page; "" = no icon
        "config_repo_label": "",          # e.g. "my-agent-config (private)"
    },
    "paths": {
        "hermes_home": "~/.hermes",
        "out_dir": "./public",            # index.html + index.<lang>.html + connectors.html
        "venv_python": "",                # python of the running Hermes venv (config map, quota)
        "node": "",                       # optional: node binary for the config-map JS self-check
        "budgets_env": "dashboard/budgets.env",
        "config_descriptions": {},        # {"en": "dashboard/config-descriptions.en.json", "ru": ...}
        "history_csv": "cache/dashboard-history.csv",
        "settings_url": "settings/",      # link in the sidebar; "" hides it
    },
    "i18n": {"default": "en", "languages": ["en", "ru"]},
    "timezone": {"name": "UTC", "offset_hours": 0, "label": "UTC"},
    # No provider is assumed: the engine cannot know which billing_provider values
    # your installation writes, and a wrong guess would silently label real money as
    # free (or the reverse). Everything here is empty until dashboard.json says
    # otherwise — see dashboard.example.json for a filled-in example.
    "providers": {
        "primary": {
            "id": "",                           # exact sessions.billing_provider of the main model
            "label": "Primary",
            "billing": "subscription",          # subscription | usage
            "subscription_usd_month": 0,        # fixed $/month, 0 = no fixed part
            "weekly_input_budget": 0,           # scale of the weekly bar, 0 = no bar
            "ref_in_per_m": 0,                  # market reference tariff, 0 = do not estimate savings
            "ref_out_per_m": 0,
            "quota_cache": "",                  # JSON from the quota collector, empty = no quota card
        },
        # Paid (usage-billed) providers. `model_like` / `exclude_models` narrow the
        # billing_provider match when one provider id carries both a paid and a
        # free tier (a paid model slot vs a free key on the same provider).
        "paid": [],
        # Free tiers (never counted as money, never an incident)
        "free": [],
        # Sources where the primary model answers by default: a paid session from
        # these is a *forced* fallback (incident). CLI runs on a paid key are deliberate.
        "fallback_sources": ["telegram", "cron", "line"],
        "cost_fresh_days": 6,
    },
    "sources": {                           # labels for sessions.source
        "telegram": "Telegram", "cron": "Cron · autonomous", "cli": "CLI",
        "subagent": "Subagents · delegate_task", "tool": "Tool sessions",
        "tui": "TUI", "line": "LINE", "api": "API",
    },
    "chats": {"names": {}},               # {"<chat id>": "Family chat"} — otherwise display_name from the DB
    "usage": {
        # Free OAuth connectors counted from messages.tool_name (LIKE patterns)
        "oauth_groups": [
            {"label": "Sheets / Drive / Docs", "like": "mcp_google_workspace%", "not_like": "%gmail%"},
            {"label": "Calendar", "like": "mcp_google_calendar%"},
            {"label": "Gmail", "like": "%gmail%"},
        ],
        # Collapse technical tool names into meaningful groups for "top tools"
        "tool_groups": [
            {"prefix": "mcp_google_workspace", "label": "Google Sheets/Drive"},
            {"prefix": "mcp_google_calendar", "label": "Google Calendar"},
            {"prefix": "mcp_notion", "label": "Notion"},
            {"prefix": "tg_", "label": "Telegram"},
        ],
        "stt_log_regex": "Transcribed",
    },
    "cron": {
        "dream_job_id": "",                # nightly memory consolidation job → green event
        "watchdogs": [],                   # [{"id": "...", "label": "Tirith watchdog", "schedule": "every 15 min"}]
        "name_strip_prefixes": [],         # e.g. ["Agent:", "Family:"] — owner prefixes cut from job names
        "categories": [                    # first keyword match wins; key → CSS colour class
            {"key": "dream", "keywords": ["dream", "сон"], "class": "cy", "legend": {"en": "memory consolidation", "ru": "консолидация памяти"}},
            {"key": "report", "keywords": ["report", "digest", "отчёт", "обзор", "дайджест"], "class": "am", "legend": {"en": "analytics & reports", "ru": "аналитика и отчёты"}},
        ],
        "default_category": {"class": "lime", "legend": {"en": "reminders & other", "ru": "напоминания и прочее"}},
        "system_category": {"class": "n", "legend": {"en": "system", "ru": "система"}},
        # system crontab lines to plot: substring of the command → label
        "system_jobs": [
            {"match": "git-autosync", "label": {"en": "Config autosync", "ru": "Автосинк GitHub"}},
            {"match": "hermes-dashboard", "label": {"en": "Dashboard regeneration", "ru": "Регенерация дашборда"}},
        ],
    },
    "security": {
        "shield_file": "",                 # e.g. "SHIELD.md" — its git date is shown
        "shield_bullets": {"en": [], "ru": []},
        "evals_path": "",                  # e.g. "evals/prompt-injection/evals.json"
        "evals_bullets": {"en": [], "ru": []},
        "log_files": ["logs/gateway.log", "logs/gateway.log.1", "logs/agent.log",
                      "logs/agent.log.1", "logs/errors.log", "logs/errors.log.1"],
        "tirith_regex": r"tirith\s+(?:path\s+resolved\s+to\s+None|spawn\s+failed|timed\s+out\s+after|returned\s+unexpected\s+exit\s+code)|tirith.*scanning\s+disabled",
        "alert_channel_label": "",         # e.g. "ops channel" — where the watchdog posts
    },
    "memory": {
        "memory_file": "memories/MEMORY.md",
        "user_file": "memories/USER.md",
        # Used only when config.yaml declares no limit; 0 = show «—» instead of a
        # percentage of a number nobody set.
        "memory_char_limit_default": 0,
        "user_char_limit_default": 0,
        "backup_branch": "",               # e.g. "agent/state"
    },
    "connectors": {
        "skills_from_gitignore": True,     # "our" skills = allowlist entries !/skills/<name>/ in .gitignore
        "descriptions": {},                # {"sheets": {"en": "...", "ru": "..."}}
        "extra_cards": [],                 # curated cards: {"section": "channels|plugins|web|candidates", "nm":..., "sv":..., "status":..., "ds": {"en":..,"ru":..}}
    },
    "views": {
        # Google Fonts is the only request a page makes to anything but your own
        # server. Off = system fonts and a page that phones nobody.
        "web_fonts": True,"config_map": True, "connectors": True},
    "config_map": {"truth_ref": "origin/main"},
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


_ENV_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")
# A language code is a filename component, a JS object key and a cookie value;
# one definition of what it may contain, shared by everything that parses one.
_LOCALES = Path(__file__).parent / "locales"
LANG_CHARS = r"[A-Za-z0-9_-]{1,8}"
_LANG_RE = re.compile("^" + LANG_CHARS + "$")


# Names that look like somebody's credentials. The dashboard's own children
# need exactly one credential family (the Anthropic admin key, below); anything
# else matching this belongs to the agent, not to us.
_SECRET_ENV_RE = re.compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|_AUTH|OAUTH|SESSION|COOKIE|"
    r"^SSH_|^AWS_|^AZURE_|^GCP_|^GOOGLE_APPLICATION|^GITHUB_|^GH_)", re.I)

# Read from the environment by our own collectors — must survive the strip.
_CHILD_ENV_KEEP = ("HERMES_DASHBOARD_ANTHROPIC_ADMIN_KEY", "ANTHROPIC_ADMIN_KEY")


def child_environment(home: Path, extra: dict | None = None) -> dict:
    """Environment for a process the dashboard spawns, minus other people's secrets.

    This is a *denylist*, deliberately, and the choice is worth explaining. A
    default-deny allowlist is the stronger shape, but it has to enumerate
    everything an unknown host needs to work at all: PATH, HOME, the TLS trust
    store (SSL_CERT_FILE, REQUESTS_CA_BUNDLE), proxy variables, VIRTUAL_ENV,
    locale, TMPDIR, and on Windows SYSTEMROOT/COMSPEC/PATHEXT without which a
    subprocess cannot even start. Miss one and a collector fails on somebody
    else's machine, silently — which is the failure mode this dashboard exists
    to prevent. A missed *secret* name leaks nothing by itself: the child is
    our own code, and the point is only to shrink what it could ever spill.

    So: keep the environment, drop what is shaped like a credential, then put
    back the one credential our own collector documents that it reads.
    """
    keep = {k: v for k, v in os.environ.items() if not _SECRET_ENV_RE.search(k)}
    for name in _CHILD_ENV_KEEP:
        if name in os.environ:
            keep[name] = os.environ[name]
    keep["HERMES_HOME"] = str(home)
    for k, v in (extra or {}).items():
        keep[str(k)] = str(v)
    return keep


def load_budgets_env(cfg: "Config") -> dict[str, str]:
    """Read paths.budgets_env (KEY=value shell file) into os.environ.

    A variable already present in the environment wins, so a wrapper that
    `source`s the file first behaves exactly as before. Missing file → {}.
    Values are trimmed of a trailing `# comment` and surrounding quotes.
    """
    p = cfg.path_in_home(cfg.get("paths.budgets_env", "dashboard/budgets.env"))
    loaded: dict[str, str] = {}
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return loaded
    for line in text.splitlines():
        m = _ENV_LINE_RE.match(line)
        if not m:
            continue
        val = m.group(2).partition("#")[0].strip().strip("'\"")
        loaded[m.group(1)] = val
        os.environ.setdefault(m.group(1), val)
    return loaded


class Config:
    """Loaded dashboard.json merged over DEFAULTS, plus resolved paths."""

    def __init__(self, data: dict | None = None, path: Path | None = None):
        self.raw = _deep_merge(DEFAULTS, data or {})
        self.path = path
        home = os.environ.get("HERMES_HOME") or self.raw["paths"]["hermes_home"]
        self.home = Path(os.path.expanduser(home))

    # ── access ────────────────────────────────────────────────────────
    def get(self, dotted: str, default=None):
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def __getitem__(self, key: str):
        return self.raw[key]

    def number(self, dotted: str, default=0, cast=float):
        """A numeric setting, or `default` if it is not one.

        validate() rejects a non-number when the form saves it, but
        dashboard.json is edited by hand; without this the read path raises and
        (since v0.4.0 contains sections) the card silently disappears instead
        of showing the default it was going to show anyway.
        """
        raw = self.get(dotted, default)
        try:
            return cast(raw)
        except (TypeError, ValueError):
            return cast(default)

    def path_in_home(self, rel: str) -> Path:
        p = Path(os.path.expanduser(str(rel)))
        return p if p.is_absolute() else self.home / p

    def text(self, value, lang: str) -> str:
        """Localised string from {"en":..,"ru":..} or plain str."""
        if isinstance(value, dict):
            return str(value.get(lang) or value.get("en") or next(iter(value.values()), ""))
        return str(value or "")

    def list_text(self, value, lang: str) -> list[str]:
        if isinstance(value, dict):
            return list(value.get(lang) or value.get("en") or [])
        return list(value or [])

    # ── derived ───────────────────────────────────────────────────────
    @property
    def out_dir(self) -> Path:
        return Path(os.path.expanduser(str(self.raw["paths"]["out_dir"])))

    @property
    def languages(self) -> list[str]:
        langs = list(self.raw["i18n"].get("languages") or ["en"])
        d = self.raw["i18n"].get("default") or langs[0]
        return [d] + [l for l in langs if l != d]

    @property
    def default_lang(self) -> str:
        return self.languages[0]

    @property
    def venv_python(self) -> str:
        p = self.raw["paths"].get("venv_python") or ""
        return os.path.expanduser(p) if p else ""

    def validate(self) -> list[str]:
        """Human-readable problems (empty list = fine). Never raises."""
        errs: list[str] = []
        # providers.primary.id is deliberately NOT required: a fresh install saves
        # the form before it knows the value, and an empty id simply attributes no
        # traffic to a primary provider. What must be right is anything that would
        # silently produce a wrong number.
        for i, p in enumerate(self.get("providers.paid", [])):
            if not p.get("id"):
                errs.append(f"providers.paid[{i}].id is required")
            for k in ("in_per_m", "out_per_m"):
                try:
                    float(p.get(k, 0))
                except (TypeError, ValueError):
                    errs.append(f"providers.paid[{i}].{k} must be a number")
        for key, cast in (("providers.primary.subscription_usd_month", float),
                          ("providers.primary.weekly_input_budget", float),
                          ("providers.primary.ref_in_per_m", float),
                          ("providers.primary.ref_out_per_m", float),
                          ("providers.cost_fresh_days", int),
                          ("memory.memory_char_limit_default", int),
                          ("memory.user_char_limit_default", int)):
            raw = self.get(key, 0)
            try:
                cast(raw)
            except (TypeError, ValueError):
                errs.append(f"{key} must be a number (got {raw!r})")
        try:
            off = int(self.get("timezone.offset_hours", 0))
            # datetime.timezone() rejects anything outside (-24h, 24h); saving 24
            # used to pass validation and then kill the build on the next run.
            if not -23 <= off <= 23:
                errs.append("timezone.offset_hours must be between -23 and 23")
        except (TypeError, ValueError):
            errs.append("timezone.offset_hours must be an integer")
        for key in ("usage.stt_log_regex", "security.tirith_regex"):
            pat = self.get(key, "")
            if pat:
                try:
                    re.compile(str(pat))
                except re.error as e:
                    errs.append(f"{key} is not a valid regular expression ({e})")
        for lang in self.raw["i18n"].get("languages") or []:
            # A language code becomes part of a filename (index.<lang>.html) and
            # part of a JS object key, so restrict the charset rather than the
            # length: the old length-only check let "../x" and 'a"b' through and
            # only stopped a path traversal by accident.
            if not isinstance(lang, str) or not _LANG_RE.match(lang):
                errs.append(f"i18n.languages: bad entry {lang!r} "
                            "(letters, digits, - and _ only, up to 8 characters)")
            elif lang != "en" and not (_LOCALES / f"{lang}.json").is_file():
                # Otherwise the build cheerfully writes index.<lang>.html full of
                # English and labels it <html lang="de"> — the switcher offers a
                # translation that does not exist.
                errs.append(f"i18n.languages: {lang!r} has no locales/{lang}.json")
        return errs


def find_config_path(explicit: str | None = None) -> Path | None:
    cands = [explicit, os.environ.get("HERMES_DASHBOARD_CONFIG")]
    home = Path(os.path.expanduser(os.environ.get("HERMES_HOME", "~/.hermes")))
    cands.append(str(home / "dashboard" / "dashboard.json"))
    for c in cands:
        if c and Path(os.path.expanduser(c)).is_file():
            return Path(os.path.expanduser(c))
    return None


def load_config(explicit: str | None = None) -> Config:
    p = find_config_path(explicit)
    data = {}
    if p:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise SystemExit(f"hermes-dashboard: cannot read {p}: {e}")
        if not isinstance(data, dict):
            raise SystemExit(f"hermes-dashboard: {p} must contain a JSON object")
    return Config(data, p)


_CURRENT: Config | None = None


def current() -> Config:
    """Process-wide config (generators call this instead of threading it through)."""
    global _CURRENT
    if _CURRENT is None:
        _CURRENT = load_config()
    return _CURRENT


def set_current(cfg: Config) -> None:
    global _CURRENT
    _CURRENT = cfg
