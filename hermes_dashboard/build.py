"""Build the dashboard pages: collect data once, render every configured language.

    python -m hermes_dashboard.build [--config dashboard.json] [--out DIR] [--lang xx]

Order per run: collectors (quota / billing caches, fail-open) → host facts →
KPIs → generators → pages. Everything degrades: a missing DB gives "—", a
failed generator gives an empty section and a line on stderr — never a broken
page with HTTP 200.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import (gen_banner, gen_connectors, gen_cron, gen_events, gen_security, gen_usage,
               history, render, sysinfo)
from .common import (active, deliberate_paid, esc, fallback, fmt_tok, home, paid, scalar,
                     yaml_get)
from .config import Config, child_environment, load_budgets_env, load_config, set_current
from .i18n import _, set_lang


# ── data that does not depend on the language ─────────────────────────

def collect_kpis(cfg: Config) -> dict:
    W7 = "started_at >= strftime('%s','now','-7 days')"
    q = {
        "SESS7": f"SELECT count(*) FROM sessions WHERE {active()} AND {W7}",
        "PASSIVE7": f"SELECT count(*) FROM sessions WHERE NOT ({active()}) AND {W7}",
        "CRON7": f"SELECT count(*) FROM sessions WHERE source='cron' AND {active()} AND {W7}",
        "FALLBACK7": f"SELECT count(*) FROM sessions WHERE {fallback(cfg)} AND {W7}",
        "PAIDCLI7": f"SELECT count(*) FROM sessions WHERE {deliberate_paid(cfg)} AND {W7}",
        "TOK7_IN": f"SELECT coalesce(sum(input_tokens),0) FROM sessions WHERE {W7}",
        "TOK7_OUT": f"SELECT coalesce(sum(output_tokens),0) FROM sessions WHERE {W7}",
        "TOK7_REASON": f"SELECT coalesce(sum(reasoning_tokens),0) FROM sessions WHERE {W7}",
        "TOK7_CALLS": f"SELECT coalesce(sum(api_call_count),0) FROM sessions WHERE {W7}",
        "TOK7_CRON": f"SELECT coalesce(sum(input_tokens),0) FROM sessions WHERE source='cron' AND {W7}",
        "MSGS": "SELECT count(*) FROM messages",
        "SESS": f"SELECT count(*) FROM sessions WHERE {active()}",
        "MEMCALLS": "SELECT count(*) FROM messages WHERE tool_name='memory' AND timestamp >= strftime('%s','now','-30 days')",
    }
    out = {k: scalar(sql, default=None) for k, sql in q.items()}
    # facts in the memory store
    facts = None
    mem_db = home() / "memory_store.db"
    if mem_db.is_file():
        import sqlite3
        try:
            with sqlite3.connect(f"file:{mem_db}?mode=ro", uri=True) as db:
                facts = db.execute("SELECT count(*) FROM facts").fetchone()[0]
        except sqlite3.Error:
            facts = None
    out["FACTS"] = facts
    return out


def run_collectors(cfg: Config) -> None:
    """Refresh JSON caches (fail-open: an error leaves the previous cache)."""
    py = cfg.venv_python or sys.executable
    env = child_environment(cfg.home, extra={
        "HERMES_DASHBOARD_TZ_OFFSET_H": str(cfg.get("timezone.offset_hours", 0)),
        "HERMES_DASHBOARD_PROVIDER": str(cfg.get("providers.primary.id", "openai-codex")),
        "PYTHONPATH": str(Path(__file__).parent.parent)})
    jobs = []
    if cfg.get("providers.primary.quota_cache"):
        jobs.append([py, "-m", "hermes_dashboard.collectors.codex_quota", "--write-cache"])
    if any(p.get("cost_cache") for p in cfg.get("providers.paid", [])):
        jobs.append([sys.executable, "-m", "hermes_dashboard.collectors.anthropic_cost"])
    for cmd in jobs:
        # A missing venv python or a hung network call must not cost the whole
        # build: the page renders from the previous cache and stderr says why.
        try:
            r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                print(f"[collector] {cmd[-1]}: rc={r.returncode} {r.stderr.strip()[-300:]}", file=sys.stderr)
        except (OSError, subprocess.SubprocessError) as e:
            print(f"[collector] {cmd[-1]}: not run ({e})", file=sys.stderr)


def memory_facts(cfg: Config) -> dict:
    def chars(rel: str | Path) -> int:
        try:
            p = rel if isinstance(rel, Path) else cfg.path_in_home(str(rel))
            return len(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0

    def limit(key: str, dflt: int) -> int:
        try:
            return int(yaml_get(f"memory.{key}", "") or dflt)
        except ValueError:
            return dflt

    memch = chars(cfg.path_in_home(str(cfg.get("memory.memory_file", "memories/MEMORY.md"))))
    userch = chars(str(cfg.get("memory.user_file", "memories/USER.md")))
    memlim = limit("memory_char_limit", int(cfg.get("memory.memory_char_limit_default", 2600)))
    userlim = limit("user_char_limit", int(cfg.get("memory.user_char_limit_default", 1700)))
    try:
        ment = cfg.path_in_home(str(cfg.get("memory.memory_file", "memories/MEMORY.md"))).read_text(encoding="utf-8").count("§")
    except (OSError, ValueError):
        ment = 0
    return {"MEMCH": memch, "USERCH": userch, "MEMLIM": memlim, "USERLIM": userlim, "MEMENT": ment,
            # no declared limit → no percentage; a made-up denominator would be a lie
            "MEMPCT": memch * 100 // memlim if memlim else None,
            "USERPCT": userch * 100 // userlim if userlim else None}


def cls_pct(p) -> str:
    if p is None:
        return ""
    return "no" if p >= 97 else ("warn" if p >= 85 else "ok")


def pct_txt(p) -> str:
    return "—" if p is None else f"{p}%"


def limit_txt(cur: int, lim: int) -> str:
    return f"{cur} / {lim} " + _("chars") if lim else f"{cur} " + _("chars") + " · " + _("no limit declared")


def s(v, dash="—"):
    return dash if v is None else str(v)


# ── views ─────────────────────────────────────────────────────────────

def chain_card(cfg: Config, state: dict) -> str:
    """Routing policy: primary → paid fallbacks → free tiers, with the live active step."""
    prim = cfg.get("providers.primary", {})
    model_main = yaml_get("model.default", "")

    def node(key, rank, name, desc, price, extra=""):
        cls, pip, note = "lnk", "pip off", ""
        if key == state.get("active"):
            cls, pip, note = "lnk act", "pip on", f' · <b>{_("answering now")}</b>'
        if key == "primary" and state.get("primary") == "cooldown":
            pip = "pip bad"
            note = (f' · <b>{_("unavailable until {t}").format(t=esc(state.get("reset")))}</b>' if state.get("reset")
                    else f' · <b>{_("unavailable")}</b>')
        if extra:
            cls += " " + extra
        return (f'<div class="{cls}"><span class="{pip}"></span><div class="w"><div class="r">{esc(rank)}</div>'
                f'<div class="n">{esc(name)}</div><div class="d">{esc(desc)}{note}</div></div><div class="c">{esc(price)}</div></div>')

    sub = float(prim.get("subscription_usd_month", 0) or 0)
    nodes = node("primary", _("primary · decisions"), f'{prim.get("label", "")} {model_main}'.strip(),
                 _("analysis, memory, reasoning cron"), f"${sub:.0f}/" + _("mo") if sub else _("usage"))
    for i, p in enumerate(cfg.get("providers.paid", []), 1):
        nodes += node(p["id"], _("reserve {n} · paid").format(n=i), str(p.get("label", p["id"])),
                      _("usage-billed API"), f"${float(p.get('in_per_m', 0)):.2f}/1M", "pay")
    for f in cfg.get("providers.free", []):
        nodes += node("free", _("reserve · free"), str(f.get("label", f.get("id"))), _("free key · last resort"), "$0")
    chain = " → ".join([str(p.get("label", p["id"])) for p in cfg.get("providers.paid", [])]
                       + [str(f.get("label", f.get("id"))) for f in cfg.get("providers.free", [])])
    algo = _("<b>{p}</b> answers by default. When it is unavailable (429/529/503/connection) the request walks the chain: <b>{c}</b> — a stepwise degradation by cost and quality, the user is never left without an answer. Simple reminders run as <b>script-only cron</b> without an inference call.").format(
        p=esc(prim.get("label", "primary")), c=esc(chain) or "—")
    return (f'<div class="sec"><div class="sec-h"><h2>{_("Models and routing")}</h2><span class="ln"></span>'
            f'<span class="note">{_("primary — decisions · on failure — the fallback chain")}</span></div>'
            f'<div class="card full"><h3>{_("Routing and fallback policy")}</h3><div class="chainv">{nodes}</div>'
            f'<div class="algo">{algo}</div></div></div>')


def view_overview(cfg: Config, k: dict, deltas: dict, host: dict, lang: str) -> str:
    people = "".join(f'<span>{esc(p.get("label", ""))} · <b>{esc(p.get("handle", ""))}</b></span>'
                     for p in cfg.get("agent.people", []))
    fam = f'<div class="fam">{people}</div>' if people else ""
    fb = k.get("FALLBACK7")
    fbclass = "warn" if (fb or 0) > 0 else "ok"
    fb_sub = _("forced switches to a paid provider")
    if (k.get("PAIDCLI7") or 0) > 0:
        fb_sub = _("forced · plus {n} deliberate paid runs outside the interactive sources").format(n=k["PAIDCLI7"])
    dot = host["dot"]
    return (
        f'<section class="view on" id="v-over">'
        f'<div class="vh"><div class="kick">{esc(cfg.text(cfg.get("agent.tagline"), lang))}</div>'
        f'<h1>{_("System overview")}</h1><p>{esc(cfg.text(cfg.get("agent.description"), lang))}</p>{fam}'
        f'<div class="meta">{_("built")} <b>{esc(host["now"])}</b> · {esc(host["freq"])} · {_("sync")} <b>{esc(host["sync"])}</b> · {_("commit")} <b>{esc(host["commit"])}</b></div></div>'
        '<div class="kpis prime">'
        f'<div class="kpi ok"><div class="l"><span class="pip on" style="background:{dot}"></span>Gateway</div><div class="v">{esc(host["gwt"])}</div><div class="x">{_("uptime")} {esc(host["up"])}</div></div>'
        f'<div class="kpi"><div class="l">{_("Sessions · 7 days")}</div><div class="v">{s(k["SESS7"])}</div><div class="x">{_("with a model call")} · cron: {s(k["CRON7"])} · {_("no answer")}: {s(k["PASSIVE7"])} {deltas.get("sess7", "")}</div></div>'
        f'<div class="kpi {fbclass}"><div class="l">{_("Fallback · 7 days")}</div><div class="v">{s(fb)}</div><div class="x">{esc(fb_sub)} {deltas.get("fallback7", "")}</div></div>'
        f'<div class="kpi info"><div class="l">{_("Input · 7 days")}</div><div class="v">{fmt_tok(k["TOK7_IN"] or 0)}</div><div class="x">cron: {fmt_tok(k["TOK7_CRON"] or 0)} {deltas.get("input7d", "")}</div></div>'
        '</div><div class="kpis sub">'
        f'<div class="kpi ok"><div class="l">{_("Load 1/5/15")}</div><div class="v">{esc(host["load"])}</div><div class="x">CPU load · {esc(host["cores"])} {_("cores")}</div></div>'
        f'<div class="kpi ok"><div class="l">{_("Server disk")}</div><div class="v">{esc(host["disk"])}</div><div class="x">{_("used")} {esc(host["diskp"])}</div></div>'
        f'<div class="kpi ok"><div class="l">{_("Server RAM")}</div><div class="v">{esc(host["ram"])}</div><div class="x">{_("used / total")}</div></div>'
        f'<div class="kpi"><div class="l">{_("Messages · total")}</div><div class="v">{s(k["MSGS"])}</div><div class="x">{s(k["SESS"])} {_("sessions with a model, all time")}</div></div>'
        f'<div class="kpi info"><div class="l">{_("Output / reasoning · 7d")}</div><div class="v">{fmt_tok(k["TOK7_OUT"] or 0)} / {fmt_tok(k["TOK7_REASON"] or 0)}</div><div class="x">{s(k["TOK7_CALLS"])} API calls</div></div>'
        f'<div class="kpi info"><div class="l">{_("Facts in memory")}</div><div class="v">{s(k["FACTS"])}</div><div class="x">holographic · SQLite</div></div>'
        f'<div class="kpi"><div class="l">{_("Custom skills")}</div><div class="v">{host["skilln"]}</div><div class="x">{_("skills of this agent")}</div></div>'
        '</div>'
        + chain_card(cfg, host["chain"])
        + host["events"] + host["security"]
        + f'<div class="printonly">{esc(cfg.get("agent.name"))} · {_("system dashboard")} · {_("Overview")} · {esc(host["now"])}</div>'
        '</section>'
    )


def view_cost(cfg: Config, usage: str, analytics: str) -> str:
    prim = cfg.get("providers.primary", {})
    return (
        '<section class="view" id="v-cost">'
        f'<div class="vh"><div class="kick">{_("Money and limits")}</div><h1>{_("Cost")}</h1>'
        f'<p>{_("The {p} subscription is fixed; anything above it is paid fallback, and it should stay near zero.").format(p=esc(prim.get("label", "primary")))}</p>'
        f'<div class="meta">{_("tariffs from")} <b>dashboard.json</b> · {_("live quota from")} <b>{esc(prim.get("quota_cache", "—"))}</b></div></div>'
        + (usage or f'<div class="sec"><div class="card"><div style="color:var(--ink3);font-size:13px">{_("Usage data unavailable (state.db).")}</div></div></div>')
        + analytics
        + f'<div class="printonly">{_("system dashboard")} · {_("Cost")}</div></section>'
    )


def view_memory(cfg: Config, k: dict, m: dict, deltas: dict, host: dict) -> str:
    facts = s(k["FACTS"])
    growth = deltas.get("facts") or f'<span style="color:var(--ink3);font-size:13px">{_("history is accumulating")}</span>'
    backup = cfg.get("memory.backup_branch", "")
    meta = f'{_("limits from")} <b>config.yaml</b>' + (f' · {_("backup to branch")} <b>{esc(backup)}</b> {_("daily at")} {esc(host["sync_at"])}' if backup else "")
    return (
        '<section class="view" id="v-mem">'
        f'<div class="vh"><div class="kick">{_("Four layers")}</div><h1>{_("Memory")}</h1>'
        f'<p>{_("Two of the four layers are bounded: they have a hard character ceiling, and when it is close the nightly consolidation must compress, not append.")}</p>'
        f'<div class="meta">{meta}</div></div>'
        '<div class="kpis">'
        f'<div class="kpi info"><div class="l">Holographic</div><div class="v">{facts}</div><div class="x">{_("facts · SQLite+FTS5 · durable")}</div></div>'
        f'<div class="kpi info"><div class="l">{_("Fact growth")}</div><div class="v" style="font-size:17px">{growth}</div><div class="x">{_("holographic dynamics · 7d")}</div></div>'
        f'<div class="kpi {cls_pct(m["MEMPCT"])}"><div class="l">MEMORY.md · runtime</div><div class="v">{pct_txt(m["MEMPCT"])}</div><div class="x">{limit_txt(m["MEMCH"], m["MEMLIM"])} · {m["MEMENT"]} §</div></div>'
        f'<div class="kpi {cls_pct(m["USERPCT"])}"><div class="l">USER.md · runtime</div><div class="v">{pct_txt(m["USERPCT"])}</div><div class="x">{limit_txt(m["USERCH"], m["USERLIM"])} · bounded</div></div>'
        f'<div class="kpi"><div class="l">{_("Memory calls · 30d")}</div><div class="v">{s(k["MEMCALLS"])}</div><div class="x">memory tool · {_("read+write")}</div></div>'
        f'<div class="kpi"><div class="l">{_("Transcripts")}</div><div class="v">{s(k["MSGS"])}</div><div class="x">state.db · {_("short-term memory")}</div></div>'
        '</div>'
        + memory_flow_svg(facts, s(k["MSGS"]), pct_txt(m["MEMPCT"]))
        + f'<div class="printonly">{_("system dashboard")} · {_("Memory")}</div></section>'
    )


def memory_flow_svg(facts: str, msgs: str, mempct: str) -> str:
    return (
        f'<div class="blk" style="margin-top:16px"><h2><em>◆</em> {_("Memory flow")} <span>{_("conversation → facts → consolidation")}</span></h2><div class="body">'
        '<div class="dg">'
        f'<svg viewBox="0 0 900 400" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{esc(_("Memory pipeline: input, accumulation, nightly consolidation, durable"))}">'
        '<defs>'
        '<marker id="m-lime" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path class="mk-lime" d="M0 0L10 5L0 10z"/></marker>'
        '<marker id="m-cy" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path class="mk-cy" d="M0 0L10 5L0 10z"/></marker>'
        '<marker id="m-dim" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto"><path class="mk-dim" d="M0 0L10 5L0 10z"/></marker>'
        '</defs>'
        '<line x1="223" y1="44" x2="223" y2="236" stroke="var(--line2)" stroke-dasharray="3 5"/>'
        '<line x1="453" y1="44" x2="453" y2="236" stroke="var(--line2)" stroke-dasharray="3 5"/>'
        '<line x1="683" y1="44" x2="683" y2="236" stroke="var(--line2)" stroke-dasharray="3 5"/>'
        f'<g class="kd" text-anchor="middle"><text x="104" y="28">1 · {esc(_("INPUT"))}</text><text x="334" y="28">2 · {esc(_("ACCUMULATION — AUTOMATIC"))}</text><text x="564" y="28">3 · {esc(_("NIGHT"))}</text><text x="795" y="28">4 · {esc(_("DURABLE"))}</text></g>'
        f'<g class="node-in" style="animation-delay:.05s"><rect class="nd" x="16" y="100" width="176" height="76" rx="11"/><text class="kd" x="34" y="122">{esc(_("CHATS"))}</text><text class="tt" x="34" y="144">{esc(_("Conversation"))}</text><text class="ms" x="34" y="162">{esc(_("voice · photo · text"))}</text></g>'
        f'<g class="node-in" style="animation-delay:.14s"><rect class="nd-hub" x="246" y="52" width="176" height="76" rx="11"/><text class="kd" x="264" y="74">DURABLE</text><text class="tt" x="264" y="96">{esc(_("Holographic facts"))}</text><text class="ms" x="264" y="114">memory_store.db · {esc(facts)}</text></g>'
        f'<g class="node-in" style="animation-delay:.2s"><rect class="nd" x="246" y="148" width="176" height="76" rx="11"/><text class="kd" x="264" y="170">{esc(_("SHORT-TERM"))}</text><text class="tt" x="264" y="192">{esc(_("Transcripts"))}</text><text class="ms" x="264" y="210">state.db · {esc(msgs)}</text></g>'
        f'<g class="node-in" style="animation-delay:.28s"><rect class="nd-cy" x="476" y="90" width="176" height="96" rx="11"/><text class="kd" x="494" y="112">{esc(_("FILTER · PRE-CHECK"))}</text><text class="tt" x="494" y="134">Dreaming</text><text class="ms" x="494" y="152">{esc(_("keep what is stable,"))}</text><text class="ms" x="494" y="168">{esc(_("let go of the temporary"))}</text></g>'
        f'<g class="node-in" style="animation-delay:.36s"><rect class="nd-hub" x="706" y="52" width="178" height="76" rx="11"/><text class="kd" x="724" y="74">BOUNDED · {mempct}</text><text class="tt" x="724" y="96">MEMORY.md</text><text class="ms" x="724" y="114">{esc(_("§ rules · memory tool"))}</text></g>'
        f'<g class="node-in" style="animation-delay:.42s"><rect class="nd" x="706" y="148" width="178" height="76" rx="11"/><text class="kd" x="724" y="170">{esc(_("HUMAN-CURATED · GIT"))}</text><text class="tt" x="724" y="192">USER.md + knowledge</text><text class="ms" x="724" y="210">{esc(_("profile and references"))}</text></g>'
        f'<g class="node-in" style="animation-delay:.5s"><rect class="nd-off" x="476" y="246" width="176" height="54" rx="11"/><text class="ms" x="494" y="270">{esc(_("not stored:"))}</text><text class="ms" x="494" y="288">{esc(_("one-off events · decay"))}</text></g>'
        '<path class="fld" d="M192 128 C214 124, 222 98, 244 92" marker-end="url(#m-dim)"/>'
        '<path class="fld" d="M192 148 C214 152, 222 178, 244 184" marker-end="url(#m-dim)"/>'
        f'<text class="lbd" x="218" y="252" text-anchor="middle">{esc(_("NO DECISION"))}</text>'
        '<path class="flc draw" style="--len:70" d="M422 96 C448 102, 452 112, 474 120" marker-end="url(#m-cy)"/>'
        '<path class="flc draw" style="--len:70" d="M422 180 C448 174, 452 164, 474 156" marker-end="url(#m-cy)"/>'
        f'<text class="lbc" x="448" y="252" text-anchor="middle">{esc(_("READS THE WINDOW"))}</text>'
        '<path class="fl draw" style="--len:70" d="M652 126 C674 120, 682 98, 704 92" marker-end="url(#m-lime)"/>'
        f'<text class="lb" x="678" y="252" text-anchor="middle">{esc(_("PROMOTES"))}</text>'
        '<path class="fld" d="M564 186 L564 244" marker-end="url(#m-dim)"/>'
        '<path class="fl draw" style="--len:820" d="M790 224 C790 312, 660 348, 420 348 C238 348, 104 332, 104 178" marker-end="url(#m-lime)"/>'
        f'<text class="lb" x="430" y="372" text-anchor="middle">{esc(_("WHAT WAS PROMOTED RETURNS INTO THE NEXT CONVERSATION"))}</text>'
        '</svg></div>'
        f'<div class="dglg"><span><i style="background:var(--lime)"></i>{esc(_("managed flow — promotion"))}</span><span><i style="background:var(--cyan)"></i>{esc(_("reads the window"))}</span><span><i style="background:var(--flow-dim)"></i>{esc(_("happens by itself, no decision"))}</span></div>'
        f'<div class="algo"><b>{_("4 layers.")}</b> {_("<b>Holographic</b> (memory_store.db — facts with trust/retrieval) · <b>runtime MEMORY/USER</b> (bounded Hermes memory, changed through the memory tool; at 100% dreaming must consolidate, not append) · <b>state.db</b> (transcripts and token telemetry) · versioned references (<b>root USER/MEMORY + knowledge/</b>). <b>Flow:</b> conversation → facts; at night <b>dreaming</b> promotes stable facts through a pre-check, does not delete decays and does not store one-off events.")}</div>'
        '</div></div>'
    )


def view_cron(cfg: Config, cron_html: str, host: dict) -> str:
    return (
        '<section class="view" id="v-cron">'
        f'<div class="vh"><div class="kick">{_("Cron · schedule and price")}</div><h1>{_("Automation")}</h1>'
        f'<p>{_("Simple reminders run without a single model call. Heavy jobs are woken by prechecks: no work — the model is not touched.")}</p>'
        f'<div class="meta">{_("source")} — <b>cron/jobs.json</b> · {_("telemetry from")} <b>state.db</b></div></div>'
        + cron_html
        + f'<div class="printonly">{_("system dashboard")} · {_("Automation")}</div></section>'
    )


def config_map_html(cfg: Config, lang: str) -> str:
    """Config view: gen_config under the Hermes venv python (needs yaml + core), else last good cache."""
    py = cfg.venv_python
    html = ""
    if py and Path(py).is_file():
        env = child_environment(cfg.home, extra={"HERMES_DASHBOARD_LANG": lang,
                                                  "PYTHONPATH": str(Path(__file__).parent.parent)})
        if cfg.path:
            env["HERMES_DASHBOARD_CONFIG"] = str(cfg.path)
        try:
            r = subprocess.run([py, "-m", "hermes_dashboard.gen_config"], env=env, capture_output=True,
                               text=True, timeout=120)
            if r.stderr:
                sys.stderr.write(r.stderr)
            if r.returncode == 0:
                html = r.stdout
        except (OSError, subprocess.SubprocessError) as e:
            sys.stderr.write(f"gen_config: {e}\n")
    stale = ""
    if not html:
        cache = home() / "cache" / f"config-map.{lang}.html"
        if cache.is_file():
            html = cache.read_text(encoding="utf-8")
            stale = f'<div class="cfempty">⚠️ {_("The config map was not rebuilt on this run — the self-check failed, the last good version is shown. See the dashboard log.")}</div>'
    if not html:
        html = (f'<div class="vh"><div class="kick">{_("Configuration")}</div><h1>{_("Config")}</h1>'
                f'<p>{_("Config map unavailable: the generator did not run (paths.venv_python must point to the Hermes venv) and there is no cache.")}</p></div>')
    return (f'<section class="view" id="v-cfg">{stale}{html}'
            f'<div class="printonly">{_("system dashboard")} · {_("Config")}</div></section>')


def _safe(name: str, fn, fallback_html: str = "") -> str:
    """Run a section generator; a crash costs that section, not the page.

    The promise in the README is that a failed generator shows up in the log
    rather than as a blank dashboard — this is where that promise is kept. A
    bad regex or an unexpected schema in one card used to abort the build and
    leave yesterday's pages in place with nothing saying why.
    """
    try:
        return fn()
    except Exception as e:                       # noqa: BLE001 — deliberately broad
        print("[section] %s failed: %s: %s" % (name, type(e).__name__, e), file=sys.stderr)
        return fallback_html


# ── main ──────────────────────────────────────────────────────────────

def build_all(cfg: Config, only_lang: str | None = None, out_dir: Path | None = None) -> list[Path]:
    set_current(cfg)
    out_dir = out_dir or cfg.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    lock = _BuildLock(out_dir).__enter__()
    if not lock.held:
        return []
    load_budgets_env(cfg)   # HERMES_DASHBOARD_QUOTA_ALERT_PCT etc. reach the collectors without a wrapper
    run_collectors(cfg)
    k = collect_kpis(cfg)
    m = memory_facts(cfg)
    deltas = history.update_and_deltas({
        "facts": k.get("FACTS"), "input7d": k.get("TOK7_IN"),
        "fallback7": k.get("FALLBACK7"), "sess7": k.get("SESS7"),
    })
    chain = gen_banner.state()
    gw = sysinfo.gateway_state()
    sha, commit = sysinfo.git_head()
    disk, diskp = sysinfo.disk()
    every = sysinfo.cron_every_minutes("hermes-dashboard") or sysinfo.cron_every_minutes("dashboard-gen")
    skills = gen_connectors.our_skill_names()
    written: list[Path] = []
    for lang in cfg.languages:
        if only_lang and lang != only_lang:
            continue
        set_lang(lang)
        host = {
            "dot": "var(--ok)" if gw == "active" else "var(--no)",
            "gwt": _("running") if gw == "active" else gw,
            "up": sysinfo.uptime_text(), "load": sysinfo.loadavg(), "cores": sysinfo.cores(),
            "disk": disk, "diskp": diskp, "ram": sysinfo.ram(), "now": sysinfo.now_label(),
            "sync": sysinfo.last_sync(), "sync_at": sysinfo.cron_hhmm("autosync"),
            "sha": sha, "commit": commit,
            "freq": _("every {n} min").format(n=every) if every else _("on schedule"),
            "skilln": len(skills), "chain": chain,
            "events": _safe("events", gen_events.build),
            "security": _safe("security", gen_security.build),
        }
        usage, analytics = _safe("usage", gen_usage.build, ("", ""))
        title = f'{cfg.get("agent.name")} — {_("system dashboard")}'
        page = (render.head(cfg, title, lang)
                + render.rail(cfg, lang, "over", host["dot"], host["gwt"], host["sync"], sha, commit)
                + '<main class="work"><div class="pad">' + gen_banner.build()
                + view_overview(cfg, k, deltas, host, lang)
                + view_cost(cfg, usage, analytics)
                + view_memory(cfg, k, m, deltas, host)
                + view_cron(cfg, _safe("cron", gen_cron.build), host)
                + (config_map_html(cfg, lang) if cfg.get("views.config_map", True) else "")
                + f'<footer><span>{_("Private dashboard")} · {_("generated")} {esc(host["now"])}</span>'
                + f'<span>{esc(cfg.get("agent.config_repo_label", "")) or "hermes-dashboard"}</span></footer>'
                + '</div></main>' + render.bottom_tabs(cfg, lang, "over") + render.script() + '</body></html>')
        target = out_dir / render.page_name(lang, cfg)
        _atomic_write(target, page)
        written.append(target)
        if cfg.get("views.connectors", True):
            ctarget = out_dir / render.page_name(lang, cfg, "connectors")
            chtml = _safe("connectors", lambda: gen_connectors.build_page(cfg, lang, host))
            if chtml:
                _atomic_write(ctarget, chtml)
                written.append(ctarget)
    lock.__exit__(None, None, None)
    return written


def _atomic_write(target: Path, text: str) -> None:
    """Write via a temp file unique to this process.

    A shared `<name>.tmp` is a race: cron and a manual rebuild overlap, both
    write the same temp path, and one of them either fails to rename or
    publishes a page the other was still writing.
    """
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=target.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o644)
        # POSIX renames over an open file happily; Windows refuses while any
        # reader holds the target, which on a page served to a browser is a
        # matter of milliseconds. Retry briefly instead of losing the build.
        for attempt in range(10):
            try:
                tmp.replace(target)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.05)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


class _BuildLock:
    """Best-effort single-writer lock on the output directory.

    Not a correctness requirement (the writes are atomic on their own) but it
    stops cron and a manual rebuild from doing the same work twice and from
    publishing a half-and-half set of pages. A lock older than STALE seconds is
    assumed to belong to a build that died and is taken over.
    """

    STALE = 900

    def __init__(self, out_dir: Path):
        self.path = out_dir / ".hermes-dashboard.lock"
        self.held = False

    def __enter__(self):
        try:
            if self.path.is_file() and time.time() - self.path.stat().st_mtime > self.STALE:
                self.path.unlink(missing_ok=True)
            fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            self.held = True
        except FileExistsError:
            print("[build] another build holds " + str(self.path) + " — skipping this run", file=sys.stderr)
        except OSError as e:
            print("[build] lock unavailable (%s) — building anyway" % (e,), file=sys.stderr)
            self.held = True
        return self

    def __exit__(self, *exc):
        if self.held:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass
        return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="hermes-dashboard build")
    ap.add_argument("--config")
    ap.add_argument("--out")
    ap.add_argument("--lang")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    for e in cfg.validate():
        sys.stderr.write(f"dashboard.json: {e}\n")
    files = build_all(cfg, a.lang, Path(a.out).expanduser() if a.out else None)
    for f in files:
        sys.stderr.write(f"wrote {f} ({f.stat().st_size} bytes)\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
