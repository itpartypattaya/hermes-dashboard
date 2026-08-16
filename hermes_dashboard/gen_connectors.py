"""connectors.html — the capability map, built from the agent's real setup.

Automatic: data connectors (config.yaml mcp_servers, incl. Google Workspace
service levels), channels (platforms.*), plugins (plugins/ + plugin.yaml,
enabled/disabled from config), engine (model chain, auxiliary vision, STT,
TTS, memory provider), skills (skills/ + SKILL.md descriptions; "ours" = the
.gitignore allowlist when enabled), bundled skill packs, cron count.
Curated: dashboard.json connectors.descriptions / connectors.extra_cards.

config.yaml is read with PyYAML when available, otherwise through the Hermes
venv python (paths.venv_python) — the system python usually has no yaml.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

from .common import env_key_names, esc, home, jobs_path
from .config import Config, current
from .i18n import _, set_lang
from . import render, sysinfo

GMAIL_OK_LEVELS = {"full", "write", "readwrite", "send", "compose", "modify", "organize"}
SVC_TITLE = {"gmail": "Gmail", "drive": "Google Drive", "docs": "Google Docs",
             "sheets": "Google Sheets", "calendar": "Google Calendar", "notion": "Notion"}
PROV_NAME = {"openai-codex": "Codex", "openai": "OpenAI", "gemini": "Gemini", "nous": "Nous",
             "groq": "Groq", "anthropic": "Claude", "openrouter": "OpenRouter", "edge": "Edge TTS"}
CHANNEL_HINT = {
    "telegram": "Telegram", "discord": "Discord", "slack": "Slack", "whatsapp": "WhatsApp",
    "line": "LINE", "signal": "Signal", "matrix": "Matrix", "webhook": "Webhook", "email": "Email",
    "sms": "SMS", "mattermost": "Mattermost", "dingtalk": "DingTalk", "feishu": "Feishu",
}


def load_yaml_config() -> dict:
    p = home() / "config.yaml"
    if not p.is_file():
        return {}
    try:
        import yaml  # type: ignore
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"[connectors] config.yaml did not parse: {e}\n")
        return {}
    py = current().venv_python
    if py and os.path.isfile(py):
        try:
            r = subprocess.run([py, "-c", "import json,sys,yaml;print(json.dumps(yaml.safe_load(open(sys.argv[1],encoding='utf-8')) or {}))", str(p)],
                               capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                return json.loads(r.stdout or "{}")
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    sys.stderr.write("[connectors] no PyYAML and no venv python — capability map is minimal\n")
    return {}


def our_skill_names() -> list[str]:
    """Custom skills: .gitignore allowlist entries `!/skills/<name>/` (when enabled), else all skills/."""
    names: list[str] = []
    if current().get("connectors.skills_from_gitignore", True):
        try:
            for line in (home() / ".gitignore").read_text(encoding="utf-8").splitlines():
                m = re.match(r"!/skills/([^/]+)/\s*$", line.strip())
                if m:
                    names.append(m.group(1))
        except OSError:
            pass
        if names:
            return names
    try:
        return sorted(d for d in os.listdir(home() / "skills") if (home() / "skills" / d).is_dir())
    except OSError:
        return []


def skill_desc(name: str) -> str:
    """First sentence of the SKILL.md front-matter description."""
    try:
        txt = (home() / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r"^description:\s*(.+?)$", txt, re.M)
    if not m:
        return ""
    d = m.group(1).strip().strip('"\'')
    if d in (">", "|", ">-", "|-"):
        lines = []
        for line in txt.split(m.group(0), 1)[1].splitlines():
            if line.startswith((" ", "\t")):
                lines.append(line.strip())
            elif lines:
                break
        d = " ".join(lines)
    d = re.split(r"(?<=[.!?])\s", d, 1)[0]
    return d[:220]


def _desc(cfg: Config, key: str, lang: str, default: str) -> str:
    d = (cfg.get("connectors.descriptions", {}) or {}).get(key)
    return cfg.text(d, lang) if d else default


def google_services(y: dict):
    srv = (y.get("mcp_servers") or {}).get("google-workspace", {}) or {}
    out = {}
    for a in srv.get("args", []) or []:
        m = re.match(r"([a-z]+):([a-z_]+)$", str(a))
        if m and m.group(1) in ("drive", "docs", "sheets", "gmail", "calendar"):
            out[m.group(1)] = m.group(2)
    return out, bool(srv.get("enabled"))


def build_connectors(cfg: Config, y: dict, lang: str) -> list[dict]:
    cards = []
    mcp = y.get("mcp_servers") or {}
    gsvc, gw_enabled = google_services(y)
    for svc in ("gmail", "drive", "docs", "sheets"):
        if svc not in gsvc:
            continue
        level = gsvc[svc]
        ok = level in GMAIL_OK_LEVELS if svc == "gmail" else level in ("full", "write", "readwrite")
        status = ("ok" if ok else "part") if gw_enabled else "avail"
        ds = _desc(cfg, svc, lang, _("Google Workspace service via MCP."))
        if status == "part":
            ds += " " + _("Read-only for now.")
        cards.append({"nm": SVC_TITLE.get(svc, svc), "sv": "Google Workspace · MCP", "status": status, "ds": ds})
    for name, srv in mcp.items():
        if name == "google-workspace":
            continue
        srv = srv or {}
        title = SVC_TITLE.get(name.replace("google-", ""), name)
        cmd = srv.get("command") or srv.get("url") or ""
        cards.append({"nm": title, "sv": "MCP", "status": "ok" if srv.get("enabled") else "avail",
                      "ds": _desc(cfg, name, lang, _("MCP server ({c}).").format(c=str(cmd)[:60]))})
    return cards


def build_channels(cfg: Config, y: dict, lang: str) -> list[dict]:
    cards = []
    plats = y.get("platforms") or {}
    for key, conf in plats.items():
        conf = conf or {}
        cards.append({"nm": CHANNEL_HINT.get(key, key), "sv": _("channel"),
                      "status": "ok" if conf.get("enabled") else "avail",
                      "ds": _desc(cfg, f"channel:{key}", lang,
                                  _("Platform adapter of Hermes; enabled in config.yaml.") if conf.get("enabled")
                                  else _("Adapter available in the engine; not enabled."))})
    return cards


def build_plugins(cfg: Config, y: dict, lang: str) -> list[dict]:
    cards = []
    pl = y.get("plugins") or {}
    enabled = set(pl.get("enabled") or [])
    disabled = set(pl.get("disabled") or [])
    pdir = home() / "plugins"
    try:
        names = sorted(d for d in os.listdir(pdir) if (pdir / d).is_dir() and not d.startswith(("_", ".")))
    except OSError:
        names = []
    for n in names:
        desc, kind = "", ""
        manifest = pdir / n / "plugin.yaml"
        if manifest.is_file():
            try:
                txt = manifest.read_text(encoding="utf-8")
                m = re.search(r"^description:\s*(.*)$", txt, re.M)
                if m:
                    d = m.group(1).strip()
                    if d in (">", "|", ">-", "|-"):
                        lines = []
                        for line in txt.split(m.group(0), 1)[1].splitlines():
                            if line.startswith((" ", "\t")):
                                lines.append(line.strip())
                            elif lines:
                                break
                        d = " ".join(lines)
                    desc = re.split(r"(?<=[.!?])\s", d.strip('"\''), 1)[0][:220]
                k = re.search(r"^kind:\s*(\S+)", txt, re.M)
                kind = k.group(1) if k else ""
            except OSError:
                pass
        status = "ok" if (n in enabled or (not enabled and n not in disabled)) else "avail"
        cards.append({"nm": n, "sv": (_("plugin") + (f" · {kind}" if kind else "")), "status": status,
                      "ds": _desc(cfg, f"plugin:{n}", lang, desc or _("Custom plugin in plugins/."))})
    return cards


def _short_model(m):
    return (m or "").split("/")[-1]


def build_engine(cfg: Config, y: dict, lang: str) -> list[dict]:
    cards = []
    m = y.get("model") or {}
    if isinstance(m, str):
        m = {"default": m}
    prov, mdl = m.get("provider", ""), _short_model(m.get("default", ""))
    cards.append({"nm": f"{PROV_NAME.get(prov, prov)} {mdl}".strip(), "sv": f"{prov} · primary", "status": "ok",
                  "ds": _desc(cfg, "engine:primary", lang, _("The model for dialogue, analysis, memory and analytical cron."))})
    for i, fb in enumerate(y.get("fallback_model") or y.get("fallback_providers") or [], 1):
        p, md = fb.get("provider", ""), _short_model(fb.get("model", fb.get("default", "")))
        cards.append({"nm": f"{PROV_NAME.get(p, p)} {md}".strip(), "sv": f"{p} · fallback {i}", "status": "ok",
                      "ds": _("Reserve #{i} in the switch-over chain when the previous model fails.").format(i=i)})
    v = (y.get("auxiliary") or {}).get("vision") or {}
    if v.get("provider"):
        native = v["provider"] == prov
        cards.append({"nm": f"{PROV_NAME.get(v['provider'], v['provider'])} {_short_model(v.get('model', ''))}".strip(),
                      "sv": "vision · OCR" + (" · " + _("native, in the subscription") if native else ""), "status": "ok",
                      "ds": _desc(cfg, "engine:vision", lang, _("Recognition of photos, video and documents; the primary model analyses the result."))})
    stt = y.get("stt") or {}
    sttp = stt.get("provider", "")
    if sttp:
        sttm = _short_model(((stt.get(sttp) or {}).get("model") or (stt.get(sttp) or {}).get("model_id") or ""))
        cards.append({"nm": f"{PROV_NAME.get(sttp, sttp)} {sttm}".strip(), "sv": "STT · " + _("voice"), "status": "ok",
                      "ds": _desc(cfg, "engine:stt", lang, _("Speech recognition of voice messages addressed to the agent."))})
    tts = y.get("tts") or {}
    ttsp = tts.get("provider", "")
    if ttsp:
        voice = (tts.get(ttsp) or {}).get("voice") or (tts.get(ttsp) or {}).get("voice_id") or ""
        cards.append({"nm": f"{PROV_NAME.get(ttsp, ttsp)} {voice}".strip(), "sv": "TTS", "status": "ok",
                      "ds": _desc(cfg, "engine:tts", lang, _("Voice replies."))})
    mem = y.get("memory") or {}
    cards.append({"nm": _("Memory") + f" · {mem.get('provider', 'default')}", "sv": "SQLite + FTS5", "status": "ok",
                  "ds": _desc(cfg, "engine:memory", lang, _("Durable facts (trust/retrieval) + MEMORY.md; FTS5 search; consolidation — dreaming."))})
    return cards


def build_web(cfg: Config, y: dict, lang: str) -> list[dict]:
    keys = env_key_names()
    cards = []
    web = y.get("web") or {}
    sb = web.get("search_backend") or ("tavily" if "TAVILY_API_KEY" in keys else ("auto" if keys & {"EXA_API_KEY", "FIRECRAWL_API_KEY", "PARALLEL_API_KEY"} else ""))
    if sb:
        cards.append({"nm": _("Web search"), "sv": sb, "status": "ok",
                      "ds": _desc(cfg, "web:search", lang, _("web_search / web_extract: internet search and page reading."))})
    if "BROWSERBASE_API_KEY" in keys:
        cards.append({"nm": "Browserbase", "sv": _("cloud browser"), "status": "ok",
                      "ds": _desc(cfg, "web:browserbase", lang, _("Real Chromium in the cloud driven by the agent (navigate/click/type/screenshot → vision)."))})
    elif (y.get("browser") or {}).get("engine"):
        cards.append({"nm": _("Browser"), "sv": str((y.get("browser") or {}).get("engine")), "status": "ok",
                      "ds": _desc(cfg, "web:browser", lang, _("Browser tool of Hermes."))})
    return cards


def build_skills(cfg: Config, lang: str) -> tuple[list[dict], list[str]]:
    ours = our_skill_names()
    cards = []
    for name in sorted(ours):
        if not (home() / "skills" / name).is_dir():
            continue
        cards.append({"nm": name, "sv": _("custom skill"), "status": "ok",
                      "ds": _desc(cfg, f"skill:{name}", lang, skill_desc(name) or _("Custom skill of this agent."))})
    try:
        bundled = sorted(d for d in os.listdir(home() / "skills")
                         if (home() / "skills" / d).is_dir() and d not in set(ours) and not d.startswith((".", "_")))
    except OSError:
        bundled = []
    return cards, bundled


def cron_count() -> int:
    try:
        d = json.loads(jobs_path().read_text(encoding="utf-8"))
        return len([j for j in d.get("jobs", []) if j.get("enabled", True)])
    except (OSError, ValueError):
        return 0


def card_html(c: dict) -> str:
    badge = {"ok": ("p-ok", _("YES")), "part": ("p-part", _("PARTLY")), "avail": ("p-avail", _("AVAILABLE")),
             "no": ("p-no", _("NO"))}[c.get("status", "avail")]
    how = f'<div class="how"><b>{_("how")}:</b> {esc(c["how"])}</div>' if c.get("how") else ""
    tags = ('<div class="meta2">' + "".join(f'<span class="mtag">{esc(t)}</span>' for t in c["tags"]) + "</div>") if c.get("tags") else ""
    return (f'<div class="ccard" data-status="{c.get("status", "avail")}"><div class="top"><div>'
            f'<div class="nm">{esc(c["nm"])}</div><div class="sv">{esc(c.get("sv", ""))}</div></div>'
            f'<span class="pill {badge[0]}">{badge[1]}</span></div>'
            f'<div class="ds">{esc(c.get("ds", ""))}</div>{how}{tags}</div>')


def section_html(title: str, sub: str, cards: list[dict]) -> str:
    if not cards:
        return ""
    return (f'<div class="sec filterable"><div class="sec-h"><h2>{esc(title)}</h2><span class="ln"></span>'
            f'<span class="note">{esc(sub)}</span></div><div class="grid g3">{"".join(card_html(c) for c in cards)}</div></div>')


def kpi(n, label, cls="", pill=None):
    p = f'<span class="pill {pill[0]}">{pill[1]}</span>' if pill else ""
    return f'<div class="kpi {cls}"><div class="l">{esc(label)} {p}</div><div class="v">{n}</div></div>'


def _extra(cfg: Config, section: str, lang: str) -> list[dict]:
    out = []
    for c in cfg.get("connectors.extra_cards", []) or []:
        if c.get("section") != section:
            continue
        d = dict(c)
        d["ds"] = cfg.text(c.get("ds", ""), lang)
        d["sv"] = cfg.text(c.get("sv", ""), lang)
        d["nm"] = cfg.text(c.get("nm", ""), lang)
        if c.get("how"):
            d["how"] = cfg.text(c.get("how"), lang)
        out.append(d)
    return out


def build_page(cfg: Config, lang: str, host: dict) -> str:
    y = load_yaml_config()
    conn = build_connectors(cfg, y, lang) + _extra(cfg, "connectors", lang)
    plugins = build_plugins(cfg, y, lang) + _extra(cfg, "plugins", lang)
    channels = build_channels(cfg, y, lang) + _extra(cfg, "channels", lang)
    engine = build_engine(cfg, y, lang) + _extra(cfg, "engine", lang)
    web = build_web(cfg, y, lang) + _extra(cfg, "web", lang)
    skills, bundled = build_skills(cfg, lang)
    candidates = _extra(cfg, "candidates", lang)
    crons = cron_count()
    all_cards = conn + plugins + channels + engine + web + skills + candidates
    n_ok = sum(1 for c in all_cards if c.get("status") == "ok")
    n_avail = sum(1 for c in all_cards if c.get("status") == "avail")
    n_no = sum(1 for c in all_cards if c.get("status") in ("no", "part"))
    bundled_card = [{"nm": _("{n} bundled Hermes skill packs").format(n=len(bundled)), "sv": "builtin · engine",
                     "status": "avail", "ds": (" · ".join(bundled) + ".") if bundled else "—",
                     "how": _("shipped with the engine; not tailored to this agent.")}] if bundled else []
    asof = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    name = cfg.get("agent.name", "Hermes")
    o = [render.head(cfg, f'{name} — {_("capability map")}', lang),
         render.rail(cfg, lang, "", host["dot"], host["gwt"], host["sync"], host["sha"], host["commit"], current_page="connectors"),
         '<main class="work"><div class="pad">',
         f'<div class="vh"><div class="kick">connectors.html · {_("generated from the config")}</div>'
         f'<h1>{_("Capability map")}</h1>'
         f'<p>{_("What already works, what the engine can do, and what could be added — assembled automatically from the real config (mcp_servers, platforms, plugins/, skills/, cron) on every regeneration; nothing is maintained by hand.")}</p>'
         f'<div class="meta">{_("snapshot")} <b>{asof}</b></div></div>',
         '<div class="sec"><div class="kpis">',
         kpi(n_ok, _("working in production"), "ok", ("p-ok", _("YES"))),
         kpi(len(skills), _("custom skills"), "", ("p-ok", "OK")),
         kpi(len(bundled), _("bundled packs"), "info", ("p-avail", _("IN ENGINE"))),
         kpi(n_avail, _("engine can, disabled"), "info", ("p-avail", _("AVAIL."))),
         kpi(n_no, _("missing / candidates"), "warn", ("p-no", "+")),
         kpi(crons, _("cron jobs"), "", ("p-ok", _("ACTIVE"))),
         '</div></div>',
         f'<div class="controls"><input id="q" placeholder="{esc(_("Search by name / service / description…"))}">'
         f'<button class="fbtn on" data-f="all">{_("all")}</button><button class="fbtn" data-f="ok">{_("YES")}</button>'
         f'<button class="fbtn" data-f="part">{_("PARTLY")}</button><button class="fbtn" data-f="avail">{_("AVAILABLE")}</button>'
         f'<button class="fbtn" data-f="no">{_("NO")}</button></div>',
         section_html(_("Data connectors"), _("external services via MCP · auto from mcp_servers"), conn),
         section_html(_("Plugins and bridges"), _("custom integrations around Hermes · auto from plugins/"), plugins),
         section_html(_("Channels"), _("messengers and entry points · auto from platforms"), channels),
         section_html(_("Engine · AI · memory"), _("model chain · auto from config.yaml"), engine),
         section_html(_("Web access"), _("search / page reading / browser"), web),
         section_html(_("Custom skills"), _("auto from skills/ ({n})").format(n=len(skills)), skills),
         section_html(_("Bundled Hermes packs"), _("built-in engine skills"), bundled_card),
         section_html(_("What is missing — candidates"), _("curated in dashboard.json"), candidates),
         f'<div class="empty" id="empty">{_("Nothing matches the current filter.")}</div>',
         f'<footer><span>{_("Private dashboard")} · {_("the map is generated from the real config")}</span>'
         f'<span>{esc(cfg.get("agent.config_repo_label", "")) or "hermes-dashboard"}</span></footer>',
         '</div></main>', render.bottom_tabs(cfg, lang, "", current_page="connectors"),
         render.script(), "<script>" + render.CONNECTORS_JS + "</script></body></html>"]
    return "\n".join(o)


if __name__ == "__main__":
    cfg = current()
    lang = os.environ.get("HERMES_DASHBOARD_LANG", cfg.default_lang)
    set_lang(lang)
    host = {"dot": "var(--ok)", "gwt": "", "sync": sysinfo.last_sync(), "sha": "", "commit": ""}
    sys.stdout.write(build_page(cfg, lang, host))
