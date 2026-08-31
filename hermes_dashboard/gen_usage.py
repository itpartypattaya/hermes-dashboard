"""«API usage — tokens and cost» + «Analytics» sections.

All numbers come from state.db (sessions + messages). Money is shown by the
best available layer, always labelled:
  1. billing export / Cost API (providers.paid[].cost_cache) — only while the
     data covers the window (data_age_days ≤ providers.cost_fresh_days);
  2. the Hermes core estimate (sessions.estimated_cost_usd, cost_status='estimated');
  3. tariff × tokens (providers.paid[].in_per_m / out_per_m).
Manual readings (prepaid balances, credits typed by hand) are deliberately not
supported: they rot on the page without anyone noticing.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta

from . import gen_chats
from .common import (
    active, connect_ro, esc, eff_bar, fmt_tok, fmt_usd, free_cond, home,
    forced_fallback_of, local_day, metrics, num, primary_id, primary_model, provider_cond,
    read_json,
    remain_color, sec,
    simple_bar, sql_str, ubar, yaml_get,
)
from .config import current
from .i18n import _, date_fmt

# ── queries ─────────────────────────────────────────────────────────────

AGG_SQL = """
    SELECT count(*) sess,
           coalesce(sum(input_tokens),0)     in_tok,
           coalesce(sum(output_tokens),0)     out_tok,
           coalesce(sum(reasoning_tokens),0)  reason,
           coalesce(sum(cache_read_tokens),0) cache,
           coalesce(sum(tool_call_count),0)   tools,
           coalesce(sum(message_count),0)     msgs,
           coalesce(sum(api_call_count),0)    calls,
           coalesce(sum(CASE WHEN cost_status='estimated' THEN estimated_cost_usd END),0) core_usd,
           sum(CASE WHEN cost_status='estimated' THEN 1 ELSE 0 END) core_n
    FROM sessions
    WHERE ({cond}) AND started_at >= strftime('%s','now',?)
"""

EMPTY = {"sess": 0, "in_tok": 0, "out_tok": 0, "reason": 0, "cache": 0, "tools": 0,
         "msgs": 0, "calls": 0, "core_usd": 0.0, "core_n": 0}


def agg_where(db, days: int, cond: str) -> dict:
    row = db.execute(AGG_SQL.format(cond=cond), (f"-{days} days",)).fetchone()
    d = dict(row) if row else dict(EMPTY)
    d["core_n"] = d.get("core_n") or 0
    return d


def tool_count(db, days: int, like: str, not_like: str | None = None) -> int:
    q = ("SELECT count(*) FROM messages WHERE tool_name LIKE ? "
         "AND timestamp >= strftime('%s','now',?)")
    args = [like, f"-{days} days"]
    if not_like:
        q += " AND tool_name NOT LIKE ?"
        args.append(not_like)
    return int(db.execute(q, args).fetchone()[0] or 0)


LOG_FILES = ("logs/agent.log", "logs/agent.log.1")
_TS_RE = re.compile(r"^(\d{4}-\d\d-\d\d)")


def log_count(pattern: str) -> tuple[int, str]:
    """(matching lines in agent.log(.1), 'since DD.MM' — the real start of the window).

    STT lives outside tool telemetry, so the only source is the agent log whose
    window is its rotation (a week or two), not "30 days" — say so next to the number.
    """
    total, first = 0, None
    rx = re.compile(pattern)
    for name in reversed(LOG_FILES):
        p = home() / name
        if not p.is_file():
            continue
        try:
            with p.open(encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if first is None:
                        m = _TS_RE.match(line)
                        if m:
                            first = m.group(1)
                    if rx.search(line):
                        total += 1
        except OSError:
            pass
    since = ""
    if first:
        try:
            since = _("since") + " " + date_fmt(datetime.strptime(first, "%Y-%m-%d"))
        except ValueError:
            since = ""
    return total, since


def source_breakdown(db, days: int, provider: str):
    cfg = current()
    labels = cfg.get("sources", {})
    rs = db.execute(
        "SELECT coalesce(source,'—') s, count(*) c FROM sessions "
        "WHERE billing_provider = ? AND " + active() + " AND started_at >= strftime('%s','now',?) "
        "GROUP BY s ORDER BY c DESC", (provider, f"-{days} days")).fetchall()
    return [(_(str(labels.get(r["s"], r["s"]))), r["c"]) for r in rs]


TOOL_LABELS = {
    "terminal": "Terminal / shell", "read_file": "Read files", "write_file": "Write files",
    "search_files": "Search files", "web_search": "Web search", "web_extract": "Web search",
    "memory": "Memory", "skill_view": "Skills", "vision_analyze": "Vision / OCR",
    "patch": "Code edits", "execute_code": "Code execution", "cronjob": "Cron",
    "session_search": "Session search", "speak": "Voice (TTS)", "image_generate": "Images",
    "delegate_task": "Subagents",
}


def pretty_tool(n: str) -> str:
    if n in TOOL_LABELS:
        return _(TOOL_LABELS[n])
    for g in current().get("usage.tool_groups", []):
        if g.get("prefix") and n.startswith(g["prefix"]):
            return str(g.get("label") or g["prefix"])
    return n.replace("mcp_", "").replace("_", " ")


def _reset_in_txt(hours) -> str:
    try:
        h = float(hours)
    except (TypeError, ValueError):
        return ""
    if h <= 0:
        return _("reset any moment")
    if h < 48:
        return _("in {h} h").format(h=f"{h:.0f}")
    return _("in {d} d {h} h").format(d=int(h // 24), h=int(h % 24))


def quota_card() -> str:
    """Live subscription quota of the primary provider (providers.primary.quota_cache)."""
    cfg = current()
    rel = cfg.get("providers.primary.quota_cache", "")
    data = read_json(cfg.path_in_home(rel)) if rel else None
    if not data or not data.get("windows"):
        return ""
    plan = data.get("plan") or "—"
    bars = ""
    for w in data["windows"]:
        rem = float(w.get("remaining_percent", 0))
        reset = w.get("reset_local") or w.get("reset_bkk")
        horizon = _reset_in_txt(w.get("reset_in_h"))
        sub = (_("resets {t}").format(t=esc(reset)) if reset else _("quota window"))
        if horizon:
            sub += f" · {horizon}"
        bars += (
            f'<div class="ubar"><div class="ubh"><span>{esc(_(str(w.get("label", ""))))}</span>'
            f'<b>{rem:.0f}% {_("free")}</b></div>'
            f'<div class="ubt"><span class="ubf" style="width:{max(2,min(100,rem)):.0f}%;'
            f'background:{remain_color(rem)}"></span></div>'
            f'<div class="ubp">{sub}</div></div>'
        )
    label = cfg.get("providers.primary.label", "")
    return (
        f'<div class="card full"><div class="uhead"><h3>{esc(_("{p} limits right now").format(p=label))}</h3>'
        f'<span class="pill p-ok">{esc(_("plan"))} {esc(plan)}</span></div>'
        + bars
        + f'<div class="algo" style="margin-top:12px">{esc(_("Live remainder of the subscription quota (the provider usage API). As many windows as upstream returns are shown; the window length is not labelled by hand — the horizon is computed from the reset time. When a window hits zero, answers are forced onto the paid fallback."))}</div></div>'
    )


TOKEN_TYPE_LABELS = {
    "input_cache_write_5m": "Cache write · 5m", "input_cache_write_1h": "Cache write · 1h",
    "input_cache_read": "Cache read", "input_no_cache": "Input without cache", "output": "Output",
}
TOKEN_TYPE_COLORS = {
    "input_cache_write_5m": "var(--part)", "input_cache_write_1h": "var(--part)",
    "input_cache_read": "var(--avail)", "input_no_cache": "var(--brand)", "output": "var(--ok)",
}


def _best_est(a: dict, in_rate: float, out_rate: float) -> tuple[float, str]:
    """(usd, 'core'|'tariff') — core estimate if present for every session of the window."""
    if a["sess"] and a["core_n"] == a["sess"] and a["core_usd"] > 0:
        return float(a["core_usd"]), "core"
    return a["in_tok"] / 1e6 * in_rate + a["out_tok"] / 1e6 * out_rate, "tariff"


def paid_card(p: dict, a7: dict, a30: dict, forced30: dict, cost_fresh_days: int) -> tuple[str, float]:
    """One usage-billed provider. Returns (html, usd_30d used in totals)."""
    cfg = current()
    label = str(p.get("label") or p["id"])
    in_rate, out_rate = float(p.get("in_per_m", 0)), float(p.get("out_per_m", 0))
    est30, est_src = _best_est(a30, in_rate, out_rate)
    est7, _s = _best_est(a7, in_rate, out_rate)

    cost = read_json(cfg.path_in_home(p["cost_cache"])) if p.get("cost_cache") else None
    age = (cost or {}).get("data_age_days")
    age = int(age) if isinstance(age, (int, float)) else None
    through = (cost or {}).get("data_through")
    have_export = bool(cost and "usd_30d" in cost)
    real = bool(have_export and age is not None and age <= cost_fresh_days)
    source = (cost.get("source") or "api") if real else None
    usd30 = float(cost["usd_30d"]) if real else est30
    usd7 = float(cost.get("usd_7d", 0)) if real else est7
    stale = have_export and not real

    est_lbl = (_("Hermes core estimate") if est_src == "core"
               else _("tariff estimate ${i}/${o} per 1M").format(i=f"{in_rate:.2f}", o=f"{out_rate:.2f}"))
    if real:
        sub = _("real $ over 30 days") + f" · {fmt_usd(usd7)} " + _("over 7d")
        pill = (f'<span class="pill p-part">{esc(_("PAID · billing export"))}</span>' if source == "csv"
                else f'<span class="pill p-part">{esc(_("PAID · Cost API"))}</span>')
        src_note = _("Real money from the {src} (the estimate would be ≈{e}).").format(
            src=_("billing export") if source == "csv" else _("Cost API"), e=fmt_usd(est30)) + " "
    else:
        sub = f"{est_lbl} · " + _("30 days") + f" · {fmt_usd(usd7)} " + _("over 7d")
        pill = f'<span class="pill p-part">{esc(_("PAID · usage billing"))}</span>'
        src_note = ((_("Hermes core estimate from its own price table (estimated_cost_usd).") if est_src == "core"
                     else _("Estimate by the public tariff (${i}/1M input, ${o}/1M output).").format(
                         i=f"{in_rate:.2f}", o=f"{out_rate:.2f}"))
                    + " " + _("Cache writes are not part of the estimate — the real bill is usually higher.") + " ")
    stale_note = ""
    if stale:
        stale_note = (
            f'<div class="cfempty" style="margin:10px 0">'
            + esc(_("A billing export exists but ends on {d} ({n} days ago) and is not used in the numbers. A fresh export replaces the estimate with real $.").format(d=through, n=age))
            + '</div>')

    extra = ""
    cache_note = ""
    by_type = ((cost or {}).get("by_type") or {}) if real else {}
    if by_type:
        items = sorted(by_type.items(), key=lambda kv: kv[1], reverse=True)
        bmax = max(v for _k, v in items) or 1
        rows_html = "".join(simple_bar(_(TOKEN_TYPE_LABELS.get(name, name)), usd, bmax,
                                       TOKEN_TYPE_COLORS.get(name, "var(--ink3)"), value_txt=fmt_usd(usd))
                            for name, usd in items)
        extra += f'<div class="cxpt" style="margin-top:14px">{esc(_("What the bill is made of · 30d"))}</div>' + rows_html
        cw = sum(v for k, v in by_type.items() if "cache_write" in k)
        if usd30 > 0 and cw > 0:
            cache_note = " " + _("Main driver of the bill — context cache writes ({c}, {p}% of real $): a token estimate does not see them.").format(
                c=fmt_usd(cw), p=f"{cw / usd30 * 100:.0f}")
    buckets = ({b.get("date"): float(b.get("usd") or 0) for b in (cost or {}).get("buckets") or []} if real else {})
    if buckets:
        days = [date.today() - timedelta(days=i) for i in range(29, -1, -1)]
        vals = [buckets.get(d.isoformat(), 0.0) for d in days]
        vmax = max(vals) or 1
        spk = ""
        for d, v in zip(days, vals):
            h = 4 if v == 0 else int(6 + v / vmax * 54)
            cls = "spk z" if v == 0 else ("spk pk" if v == vmax else "spk")
            spk += f'<div class="{cls}" style="height:{h}px" title="{d.isoformat()}: {fmt_usd(v)}"></div>'
        extra += (f'<div class="cxpt" style="margin-top:14px">{esc(_("Real $ by day · 30d"))}</div>'
                  f'<div class="spark">{spk}</div>'
                  f'<div class="sparx"><span>{date_fmt(days[0])}</span><span>{date_fmt(days[-1])}</span></div>')

    if a30["calls"] > 0:
        forced = forced30["sess"]
        delib = a30["sess"] - forced
        note = _("Fallback rank {r}.").format(r=p.get("rank", "?")) + " " + _("Over 30d: forced sessions — {f}").format(f=f"<b>{forced}</b>")
        if delib:
            note += ", " + _("deliberate (CLI, paid key) — {d} ({t} input)").format(
                d=f"<b>{delib}</b>", t=fmt_tok(a30["in_tok"] - forced30["in_tok"]))
        note += ". "
    else:
        note = _("Fallback rank {r}.").format(r=p.get("rank", "?")) + " " + _("Not triggered yet, no tokens.") + " "

    html = (
        f'<div class="card paid full"><div class="uhead"><h3>{esc(_("Paid fallback"))} · {esc(label)}</h3>{pill}</div>'
        f'<div class="bigusd">{"" if real else "≈ "}{fmt_usd(usd30)}<span>{esc(sub)}</span></div>'
        + stale_note
        + metrics([(_("Input · 7d"), fmt_tok(a7["in_tok"])), (_("Output · 7d"), fmt_tok(a7["out_tok"])),
                   (_("Input · 30d"), fmt_tok(a30["in_tok"])), (_("Requests · 30d"), str(a30["calls"]))])
        + extra
        + f'<div class="algo" style="margin-top:12px">{note}{esc(src_note)}{esc(cache_note)}</div></div>'
    )
    return html, usd30


def build() -> tuple[str, str]:
    cfg = current()
    db = connect_ro()
    if db is None:
        return "", ""
    prim = cfg.get("providers.primary", {})
    pid = primary_id(cfg)
    fresh_days = cfg.number("providers.cost_fresh_days", 6, int)
    with db:
        # active(): a session with no model call is not usage — README's first
        # honesty rule — and would otherwise inflate the session count here.
        # sql_str(): the id is a config value like any other — paid()/free_cond()
        # quote theirs, and hand-rolling this one is how a stray apostrophe turns
        # into broken SQL and a missing section.
        prim_cond = "billing_provider=" + sql_str(pid) + " AND " + active()
        cx7 = agg_where(db, 7, prim_cond)
        cx30 = agg_where(db, 30, prim_cond)
        cx_src = source_breakdown(db, 30, pid)
        paid_data = []
        for p in cfg.get("providers.paid", []):
            if not p.get("id"):
                continue
            c = provider_cond(p)
            paid_data.append((p, agg_where(db, 7, c), agg_where(db, 30, c),
                              agg_where(db, 30, forced_fallback_of(c, cfg))))
        free30 = agg_where(db, 30, free_cond(cfg))
        oauth = [(g.get("label", "?"), tool_count(db, 30, g.get("like", "%"), g.get("not_like")))
                 for g in cfg.get("usage.oauth_groups", [])]
        vision = tool_count(db, 30, "vision_analyze")
        tts = tool_count(db, 30, "speak")
        web = tool_count(db, 30, "web_search") + tool_count(db, 30, "web_extract")
        img = tool_count(db, 30, "image_generate")
        tool_rows = db.execute(
            "SELECT tool_name, count(*) c FROM messages WHERE tool_name IS NOT NULL AND tool_name != '' "
            "AND timestamp >= strftime('%s','now','-30 days') GROUP BY tool_name").fetchall()
        day_rows = db.execute(
            "SELECT " + local_day() + " d, count(*) c FROM sessions "
            "WHERE " + active() + " AND started_at >= strftime('%s','now','-30 days') GROUP BY d").fetchall()
        day_tok_rows = db.execute(
            "SELECT " + local_day() + " d, coalesce(sum(input_tokens),0) i,"
            " coalesce(sum(output_tokens),0) o, coalesce(sum(reasoning_tokens),0) r,"
            " coalesce(sum(cache_read_tokens),0) c FROM sessions "
            "WHERE started_at >= strftime('%s','now','-30 days') GROUP BY d").fetchall()
    db.close()
    stt, stt_since = log_count(str(cfg.get("usage.stt_log_regex", "Transcribed")))
    model_main = primary_model()
    prim_label = str(prim.get("label") or pid)
    # dashboard.json is the source; budgets.env (loaded into the env by the build)
    # only fills a zero, so an alert cron and the bar can share one number.
    sub_usd = float(prim.get("subscription_usd_month", 0) or os.environ.get("CODEX_SUB_USD_MO", 0) or 0)
    weekly_budget = int(float(prim.get("weekly_input_budget", 0) or os.environ.get("CODEX_WEEKLY_INPUT_BUDGET", 0) or 0))

    # ── primary card ─────────────────────────────────────────────────
    cache_hit = (cx30["cache"] / (cx30["cache"] + cx30["in_tok"]) * 100) if (cx30["cache"] + cx30["in_tok"]) else 0
    reason_share = (cx30["reason"] / cx30["out_tok"] * 100) if cx30["out_tok"] else 0
    total_proc30 = cx30["in_tok"] + cx30["out_tok"] + cx30["reason"] + cx30["cache"]
    avg_tok = ((cx30["in_tok"] + cx30["out_tok"] + cx30["reason"]) / cx30["sess"]) if cx30["sess"] else 0
    avg_calls = (cx30["calls"] / cx30["sess"]) if cx30["sess"] else 0
    smax = max((c for _l, c in cx_src), default=1)
    pal = ["var(--brand)", "var(--avail)", "var(--ok)", "var(--part)", "var(--no)", "var(--brand-d)"]
    src_bars = "".join(simple_bar(l, c, smax, pal[i % len(pal)]) for i, (l, c) in enumerate(cx_src))
    is_sub = prim.get("billing", "subscription") == "subscription"
    pill = (_("subscription · ${u}/mo · tokens on top $0").format(u=f"{sub_usd:.0f}") if is_sub else _("usage billing"))
    c_primary = (
        f'<div class="card full"><div class="uhead"><h3>{esc(_("Primary model"))} · {esc(prim_label)} {esc(model_main)}</h3>'
        f'<span class="pill p-ok">{esc(pill)}</span></div>'
        + (ubar(_("Input · 7 days"), cx7["in_tok"], weekly_budget, _("of the weekly reference budget")) if weekly_budget else "")
        + metrics([(_("Input · 7d"), fmt_tok(cx7["in_tok"])), (_("Output · 7d"), fmt_tok(cx7["out_tok"])),
                   (_("Reasoning · 7d"), fmt_tok(cx7["reason"])), (_("From cache · 7d"), fmt_tok(cx7["cache"])),
                   (_("Requests · 7d"), num(cx7["calls"])), (_("Tool calls · 7d"), num(cx7["tools"])),
                   (_("Messages · 7d"), num(cx7["msgs"])), (_("Sessions · 7d"), str(cx7["sess"]))])
        + '<div class="cxsplit">'
        + f'<div class="cxpanel"><div class="cxpt">{esc(_("Efficiency · 30 days"))}</div>'
        + eff_bar(_("Context cache"), cache_hit, _("{t} read from cache — context is not recomputed").format(t=fmt_tok(cx30["cache"])), "var(--ok)")
        + eff_bar(_("Reasoning to output"), reason_share, _("{t} reasoning tokens before answers").format(t=fmt_tok(cx30["reason"])), "var(--avail)")
        + '<div class="umrow" style="grid-template-columns:repeat(3,1fr);margin-top:13px">'
        + f'<div class="um"><div class="uml">{esc(_("Processed · 30d"))}</div><div class="umv">{fmt_tok(total_proc30)}</div></div>'
        + f'<div class="um"><div class="uml">{esc(_("Avg tokens/session"))}</div><div class="umv">{fmt_tok(avg_tok)}</div></div>'
        + f'<div class="um"><div class="uml">{esc(_("Avg requests/session"))}</div><div class="umv">{avg_calls:.0f}</div></div>'
        + '</div></div>'
        + f'<div class="cxpanel"><div class="cxpt">{esc(_("Session sources · 30 days"))}</div>' + src_bars
        + f'<div class="algo" style="margin-top:12px;border-top:0;padding-top:0">{esc(_("Where the work comes from: chats, autonomous cron, CLI and subagents."))}</div></div>'
        + '</div>'
        + f'<div class="algo" style="margin-top:16px">'
        + esc(_("Over 30 days: {i} input · {o} output · {r} reasoning · {c} requests · {t} tool calls. Processed ≈{p} tokens ({h}% from cache).").format(
            i=fmt_tok(cx30["in_tok"]), o=fmt_tok(cx30["out_tok"]), r=fmt_tok(cx30["reason"]), c=num(cx30["calls"]),
            t=num(cx30["tools"]), p=fmt_tok(total_proc30), h=f"{cache_hit:.0f}"))
        + (" " + esc(_("No per-token charge — the whole volume is inside the fixed subscription (${u}/mo); the bar is a reference, not a hard limit.").format(u=f"{sub_usd:.0f}")) if is_sub else "")
        + '</div></div>'
    )

    # ── paid providers ───────────────────────────────────────────────
    paid_html = ""
    real_paid30 = 0.0
    paid_calls30 = 0
    for p, a7, a30, fb30 in paid_data:
        h, usd = paid_card(p, a7, a30, fb30, fresh_days)
        paid_html += h
        real_paid30 += usd
        paid_calls30 += a30["calls"]

    # ── free tiers ───────────────────────────────────────────────────
    c_free = ""
    if cfg.get("providers.free"):
        labels = ", ".join(str(f.get("label") or f.get("id")) for f in cfg.get("providers.free", []))
        c_free = (
            f'<div class="card"><div class="uhead"><h3>{esc(_("Free tiers"))}</h3>'
            f'<span class="pill p-ok">{esc(_("$0 per token"))}</span></div>'
            + metrics([(_("Input · 30d"), fmt_tok(free30["in_tok"])), (_("Requests · 30d"), str(free30["calls"])),
                       (_("Sessions · 30d"), str(free30["sess"]))])
            + f'<div class="algo" style="margin-top:12px">{esc(labels)}. {esc(_("Free keys are never counted as money and never as an incident."))}</div></div>'
        )

    # ── OAuth connectors ─────────────────────────────────────────────
    c_oauth = ""
    if oauth:
        gmax = max([v for _l, v in oauth] + [1])
        bars = "".join(simple_bar(l, v, gmax, pal[i % len(pal)]) for i, (l, v) in enumerate(oauth))
        c_oauth = (
            f'<div class="card"><div class="uhead"><h3>{esc(_("OAuth connectors"))}</h3>'
            f'<span class="pill p-ok">{esc(_("free quotas"))}</span></div>' + bars
            + f'<div class="algo" style="margin-top:12px">{esc(_("Calls over 30 days (messages.tool_name). OAuth to your own account — no per-call charge, within the free quotas; not the paid model API above."))}</div></div>'
        )

    # ── auxiliary providers ──────────────────────────────────────────
    amax = max(vision, stt, web, tts, img, 1)
    vis_prov = yaml_get("auxiliary.vision.provider", "") or _("provider per config")
    vis_bill = (_("inside the primary subscription (images go native)") if vis_prov == pid
                else _("by provider {p}").format(p=vis_prov))
    c_aux = (
        f'<div class="card"><div class="uhead"><h3>{esc(_("Auxiliary · recognition"))}</h3>'
        f'<span class="pill p-avail">{esc(_("not for decisions"))}</span></div>'
        + simple_bar(f"Vision / OCR · {vis_prov}", vision, amax, "var(--avail)")
        + simple_bar((_("STT · speech · log {s}").format(s=stt_since)).rstrip(), stt, amax, "var(--brand)")
        + simple_bar(_("Web · search + extract"), web, amax, "var(--ok)")
        + simple_bar(_("TTS · voice"), tts, amax, "var(--part)")
        + simple_bar(_("Images"), img, amax, "var(--no)")
        + f'<div class="algo" style="margin-top:12px">'
        + esc(_("Calls over 30 days from state.db; STT — from agent.log (the log rotation window, {s}, not 30 days). Vision/OCR is billed {b} (auxiliary.vision); STT and the rest are auxiliary recognition providers (see the Config view: stt.provider, tts.provider), not the model that decides.").format(
            s=stt_since or _("start unknown"), b=vis_bill))
        + '</div></div>'
    )

    usage = sec(_("API usage — tokens and cost"), _("state.db · 7 and 30 days · real data"),
                '<div class="grid us2">' + c_primary + quota_card() + paid_html + c_free + c_oauth + c_aux + '</div>')

    # ════════════════ analytics ════════════════
    codex_would = (cx30["in_tok"] / 1e6 * float(prim.get("ref_in_per_m", 0))
                   + (cx30["out_tok"] + cx30["reason"]) / 1e6 * float(prim.get("ref_out_per_m", 0)))
    real_total30 = (sub_usd if is_sub else 0) + real_paid30
    free_calls = cx30["calls"] + free30["calls"]
    total_calls = free_calls + paid_calls30
    paid_share = (paid_calls30 / total_calls * 100) if total_calls else 0
    c_econ = (
        f'<div class="card paid"><div class="uhead"><h3>{esc(_("Economics · 30 days"))}</h3>'
        f'<span class="pill p-part">{esc(_("real money"))}</span></div>'
        f'<div class="bigusd">≈ {fmt_usd(real_total30)}<span>{esc(_("spent · subscription ${u} + paid fallback").format(u=f"{sub_usd:.0f}"))}</span></div>'
        + metrics([(_("Subscription"), f"${sub_usd:.0f}/" + _("mo")), (_("Paid fallback"), f"≈{fmt_usd(real_paid30)}"),
                   (_("Covered by subscription"), f"≈{fmt_usd(codex_would)}"), (_("Paid share"), f"{paid_share:.0f}%")])
        + f'<div class="algo" style="margin-top:14px">'
        + esc(_("Total over 30 days ≈{t}: fixed subscription ${u}/mo (main traffic — {i} input, at a comparable market tariff ≈{w}) plus paid fallback ≈{p}, only when the primary was down. Another {c} tokens came from cache.").format(
            t=fmt_usd(real_total30), u=f"{sub_usd:.0f}", i=fmt_tok(cx30["in_tok"]), w=fmt_usd(codex_would),
            p=fmt_usd(real_paid30), c=fmt_tok(cx30["cache"])))
        + '</div></div>'
    )
    free_pct = (free_calls / total_calls * 100) if total_calls else 0
    fw = max(3.0, min(97.0, free_pct))
    c_routing = (
        f'<div class="card"><div class="uhead"><h3>{esc(_("Routing health · 30 days"))}</h3>'
        f'<span class="pill p-ok">{free_pct:.0f}% {esc(_("free"))}</span></div>'
        f'<div class="bignum">{free_pct:.0f}%<span>{esc(_("requests without per-token payment — subscription + free tiers"))}</span></div>'
        f'<div class="split"><span class="free" style="width:{fw:.0f}%"></span><span class="paid" style="width:{100 - fw:.0f}%"></span></div>'
        '<div class="spleg">'
        f'<i><span class="dot" style="background:var(--ok)"></span>{esc(_("Subscription + free"))} — {free_calls}</i>'
        f'<i><span class="dot" style="background:var(--part)"></span>{esc(_("Paid fallback"))} — {paid_calls30}</i></div>'
        f'<div class="algo" style="margin-top:14px">{esc(_("Of {t} requests over 30 days, the subscription and free tiers covered {f}; paid fallbacks — only {p}. The higher the green share, the cheaper the agent runs.").format(t=total_calls, f=free_calls, p=paid_calls30))}</div></div>'
    )

    dmap = {r["d"]: r["c"] for r in day_rows}
    today = date.today()
    days_list = [today - timedelta(days=i) for i in range(29, -1, -1)]
    counts = [dmap.get(d.isoformat(), 0) for d in days_list]
    dmax = max(counts) or 1
    act = [c for c in counts if c > 0]
    total_sess = sum(counts)
    peak = max(counts) if counts else 0
    spk = "".join(
        f'<div class="{"spk z" if c == 0 else ("spk pk" if c == peak else "spk")}" style="height:{4 if c == 0 else int(6 + c / dmax * 54)}px" title="{d.isoformat()}: {c}"></div>'
        for d, c in zip(days_list, counts))
    c_activity = (
        f'<div class="card"><div class="uhead"><h3>{esc(_("Activity · 30 days"))}</h3>'
        f'<span class="pill p-avail">{esc(_("sessions per day"))}</span></div>'
        f'<div class="spark">{spk}</div>'
        f'<div class="sparx"><span>{date_fmt(days_list[0])}</span><span>{date_fmt(days_list[-1])}</span></div>'
        + metrics([(_("Peak · day"), str(peak)), (_("Avg per day"), f"{(total_sess / len(act)) if act else 0:.0f}"),
                   (_("Active days"), str(len(act))), (_("Sessions · 30d"), str(total_sess))])
        + f'<div class="algo" style="margin-top:14px">{esc(_("How often per day the agent woke up for a task (sessions with at least one model call; watching groups without answering is not counted). Spikes — busy chat days and cron analytics; the flat floor — background reminders."))}</div></div>'
    )

    folded: dict[str, int] = {}
    for r in tool_rows:
        k = pretty_tool(r["tool_name"])
        folded[k] = folded.get(k, 0) + r["c"]
    top = sorted(folded.items(), key=lambda kv: kv[1], reverse=True)[:7]
    tmax = max((v for _k, v in top), default=1)
    palette = ["var(--brand)", "var(--avail)", "var(--ok)", "var(--part)", "var(--no)", "var(--brand-d)", "var(--ink3)"]
    c_tools = (
        f'<div class="card"><div class="uhead"><h3>{esc(_("Top tools · 30 days"))}</h3>'
        f'<span class="pill p-avail">{esc(_("what the agent does"))}</span></div>'
        + "".join(simple_bar(l, v, tmax, palette[i % len(palette)]) for i, (l, v) in enumerate(top))
        + f'<div class="algo" style="margin-top:12px">{esc(_("Tool calls from messages, collapsed by meaning (spreadsheets, messenger, memory, web…). Shows where the agent really spends actions."))}</div></div>'
    )

    tmap = {r["d"]: (r["i"], r["o"], r["r"], r["c"]) for r in day_tok_rows}
    CH = 122
    day_tot = [sum(tmap.get(d.isoformat(), (0, 0, 0, 0))) for d in days_list]
    tot_max = max(day_tot) or 1
    cols = ""
    sum_i = sum_o = sum_r = sum_c = 0
    for d, tot in zip(days_list, day_tot):
        i, o, r, c = tmap.get(d.isoformat(), (0, 0, 0, 0))
        sum_i += i; sum_o += o; sum_r += r; sum_c += c
        if tot == 0:
            cols += f'<div class="stc z" title="{d.isoformat()}: 0"></div>'
            continue
        tip = f"{d.isoformat()}: input {fmt_tok(i)} · output {fmt_tok(o)} · reasoning {fmt_tok(r)} · cache {fmt_tok(c)}"
        segs = ""
        for cls, v in (("sc", c), ("sr", r), ("so", o), ("si", i)):
            h = round(v / tot_max * CH)
            if v and h < 1:
                h = 1
            if h:
                segs += f'<i class="{cls}" style="height:{h}px"></i>'
        cols += f'<div class="stc" title="{esc(tip)}">{segs}</div>'
    tok_total30 = sum_i + sum_o + sum_r + sum_c
    peak_d, peak_v = max(zip(days_list, day_tot), key=lambda kv: kv[1])
    cache_share30 = (sum_c / tok_total30 * 100) if tok_total30 else 0
    c_daily = (
        f'<div class="card full"><div class="uhead"><h3>{esc(_("Tokens by day · 30 days"))}</h3>'
        f'<span class="pill p-avail">{esc(_("all providers · state.db"))}</span></div>'
        f'<div class="stack">{cols}</div>'
        f'<div class="sparx"><span>{date_fmt(days_list[0])}</span><span>{date_fmt(days_list[-1])}</span></div>'
        '<div class="spleg" style="margin-top:10px">'
        f'<i><span class="dot" style="background:var(--brand)"></span>Input</i>'
        f'<i><span class="dot" style="background:var(--avail)"></span>Output</i>'
        f'<i><span class="dot" style="background:var(--brand-d)"></span>Reasoning</i>'
        f'<i><span class="dot" style="background:var(--line)"></span>{esc(_("From cache"))}</i></div>'
        + metrics([(_("Total · 30d"), fmt_tok(tok_total30)), (_("Peak") + " · " + date_fmt(peak_d), fmt_tok(peak_v)),
                   (_("Avg per day"), fmt_tok(tok_total30 / 30)), (_("Cache share"), f"{cache_share30:.0f}%")])
        + f'<div class="algo" style="margin-top:14px">{esc(_("Daily token spend across all providers. Coloured segments — input/output/reasoning, grey — context read from cache (not recomputed, not re-billed). Spikes show when and with what the agent burned tokens."))}</div></div>'
    )

    analytics = sec(_("Analytics · how the agent works"), _("state.db · tokens, activity, chats · 30 days"),
                    '<div class="grid us2">' + c_econ + c_routing + c_daily + c_activity + c_tools + gen_chats.build() + '</div>',
                    sec_id="analytics")
    return usage, analytics
