"""SVG day timeline from cron/jobs.json + the system crontab, plus the cron
efficiency panel, the "cron by API cost" card and per-job token telemetry.

Hermes-cron matches expressions in the agent timezone (config.yaml), the system
crontab in the host TZ — assumed the same (dashboard.json timezone), so both
groups share one axis. System jobs are read from the real `crontab -l`.
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess

from .common import db_path, esc, fmt_tok, jobs_path, paid, primary_id, sec
from .config import current
from .i18n import _, lang

LB_CLASS = {"lime": "lb", "cy": "lbc", "am": "lba", "n": "lbd"}
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _cats():
    return current().get("cron.categories", []) or []


def categorize(name: str) -> tuple[str, str]:
    """(key, css class) by keyword; default category otherwise."""
    n = (name or "").lower()
    for c in _cats():
        if any(k.lower() in n for k in c.get("keywords", [])):
            return str(c.get("key", "other")), str(c.get("class", "lime"))
    return "other", str(current().get("cron.default_category.class", "lime"))


def strip_name(name: str) -> str:
    s = name or ""
    for pref in current().get("cron.name_strip_prefixes", []) or []:
        if s.startswith(pref):
            s = s[len(pref):].lstrip(" :—-")
    s = re.sub(r"\s*\([^)]*\)\s*$", "", s).strip()
    return s or (name or "?")


def dow_label(dow: str) -> str:
    dow = (dow or "*").strip()
    m = {"*": _("daily"), "1-5": _("weekdays"), "0,6": _("Sat, Sun"), "6,0": _("Sat, Sun"),
         "0": _("Sun"), "1": _("Mon"), "2": _("Tue"), "3": _("Wed"), "4": _("Thu"), "5": _("Fri"), "6": _("Sat")}
    return m.get(dow, dow)


def sched_label(dom: str, mon: str, dow: str) -> str:
    dom = (dom or "*").strip()
    if dom != "*":
        lbl = _("day {d}").format(d=dom)
        mon = (mon or "*").strip()
        if mon != "*":
            try:
                lbl += f" · {_(MONTHS[int(mon) - 1])}"
            except (ValueError, IndexError):
                lbl += f" · {mon}"
        return lbl
    return dow_label(dow)


def parse_cron(expr: str):
    parts = (expr or "").split()
    if len(parts) < 5:
        return None
    mn, hr, dom, mon, dow = parts[:5]
    if not re.fullmatch(r"\d{1,2}", hr) or not re.fullmatch(r"\d{1,2}", mn):
        return None
    return int(mn), int(hr), dom, mon, dow


def freq_label(expr: str) -> str:
    parts = (expr or "").split()
    if len(parts) < 5:
        return expr
    mn, hr = parts[0], parts[1]
    m = re.fullmatch(r"\*/(\d+)", mn)
    if m and hr == "*":
        return _("every {n} min").format(n=m.group(1))
    if mn == "*" and hr == "*":
        return _("every minute")
    hm = re.fullmatch(r"\*/(\d+)", hr)
    if hm and re.fullmatch(r"\d{1,2}", mn):
        return _("every {n} h").format(n=hm.group(1))
    return expr


def load_system_jobs():
    try:
        txt = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return [], []
    cfg = current()
    labels = cfg.get("cron.system_jobs", []) or []
    sys_cls = str(cfg.get("cron.system_category.class", "n"))
    plotted, background = [], []
    for line in txt.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" in line.split()[0]:
            continue
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        expr, cmd = " ".join(parts[:5]), parts[5]
        hit = next((cfg.text(s.get("label", s.get("match")), lang()) for s in labels if s.get("match") and s["match"] in cmd), None)
        if not hit:
            continue
        pc = parse_cron(expr)
        if pc:
            mn, hr, dom, mon, dow = pc
            plotted.append({"min": mn, "hour": hr, "dow": sched_label(dom, mon, dow), "name": hit,
                            "cls": sys_cls, "no_agent": True, "precheck": False})
        else:
            background.append(f"{hit.lower()} {freq_label(expr)}")
    return plotted, background


def load_jobs():
    out, background = [], []
    try:
        d = json.loads(jobs_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {"jobs": []}
    for j in d.get("jobs", []):
        if not j.get("enabled", True):
            continue
        sch = j.get("schedule") or {}
        kind = sch.get("kind")
        short = strip_name(j.get("name", ""))
        if kind == "interval":
            background.append(f"{short.lower()} " + _("every {n} min").format(n=sch.get("minutes", "?")))
            continue
        if kind != "cron":
            continue
        pc = parse_cron(sch.get("expr", ""))
        if not pc:
            background.append(f"{short.lower()} {freq_label(sch.get('expr', ''))}")
            continue
        mn, hr, dom, mon, dow = pc
        _k, cls = categorize(j.get("name"))
        out.append({"min": mn, "hour": hr, "dow": sched_label(dom, mon, dow), "name": short, "cls": cls,
                    "no_agent": bool(j.get("no_agent")), "precheck": bool(j.get("script"))})
    sys_jobs, sys_bg = load_system_jobs()
    return out + sys_jobs, background + sys_bg


def _field_count(f: str, lo: int, hi: int) -> int:
    f = (f or "*").strip()
    if f == "*":
        return hi - lo + 1
    m = re.fullmatch(r"\*/(\d+)", f)
    if m:
        return (hi - lo) // int(m.group(1)) + 1
    n = 0
    for tok in f.split(","):
        r = re.fullmatch(r"(\d+)-(\d+)(?:/(\d+))?", tok)
        if r:
            n += (int(r.group(2)) - int(r.group(1))) // int(r.group(3) or 1) + 1
        elif tok.isdigit():
            n += 1
    return max(n, 1)


def weekly_ticks(expr: str) -> float:
    parts = (expr or "").split()
    if len(parts) < 5:
        return 0.0
    mn, hr, dom, _mon, dow = parts[:5]
    per_day = _field_count(mn, 0, 59) * _field_count(hr, 0, 23)
    if dom != "*":
        return per_day * _field_count(dom, 1, 31) / 4.35
    days = _field_count(dow, 0, 6) if dow != "*" else 7
    return float(per_day * days)


def schedule_weekly_ticks(sch: dict) -> float:
    kind = (sch or {}).get("kind")
    if kind == "cron":
        return weekly_ticks(sch.get("expr", ""))
    if kind == "interval":
        minutes = sch.get("minutes") or 0
        return 10080.0 / minutes if minutes else 0.0
    return 0.0


def cron_db_stats() -> tuple[int, int]:
    if not db_path().is_file():
        return -1, -1
    try:
        with sqlite3.connect(f"file:{db_path()}?mode=ro", uri=True) as db:
            row = db.execute("SELECT count(*), coalesce(sum(input_tokens),0) FROM sessions "
                             "WHERE source='cron' AND coalesce(api_call_count,0)>0 "
                             "AND started_at >= strftime('%s','now','-7 days')").fetchone()
            return int(row[0]), int(row[1])
    except sqlite3.Error:
        return -1, -1


def um(label, value):
    return f'<div class="um"><div class="uml">{esc(label)}</div><div class="umv">{esc(str(value))}</div></div>'


def efficiency_card() -> str:
    try:
        raw = json.loads(jobs_path().read_text(encoding="utf-8")).get("jobs", [])
    except (OSError, ValueError):
        return ""
    enabled = [j for j in raw if j.get("enabled", True)]
    script_only = [j for j in enabled if j.get("no_agent")]
    llm = [j for j in enabled if not j.get("no_agent")]
    precheck = [j for j in llm if j.get("script")]
    failed = [j for j in enabled if j.get("last_status") and j.get("last_status") != "ok"]
    noop_week = sum(schedule_weekly_ticks(j.get("schedule") or {}) for j in script_only)
    once_queue = [j for j in enabled if (j.get("schedule") or {}).get("kind") == "once"]
    sess7, tok7 = cron_db_stats()
    mrow = ('<div class="umrow">' + um(_("Jobs enabled"), len(enabled)) + um(_("Script-only · no LLM"), len(script_only))
            + um(_("LLM jobs · with a model"), len(llm)) + um(_("Failing now"), len(failed)) + '</div>')
    mrow2 = ('<div class="umrow">' + um(_("≈ runs/week without LLM"), f"{noop_week:.0f}")
             + um(_("LLM cron sessions · 7d"), "—" if sess7 < 0 else str(sess7))
             + um(_("Cron input · 7d"), "—" if tok7 < 0 else fmt_tok(tok7)) + um(_("Jobs with precheck"), len(precheck)) + '</div>')
    if failed:
        items = "".join(
            f'<li><b>{esc(j.get("name", ""))}</b> — {esc(j.get("last_status"))}'
            f'{(" · " + esc(str(j.get("last_error"))[:100])) if j.get("last_error") else ""}</li>' for j in failed)
        status_html = f'<div class="algo" style="margin-top:14px">⚠️ {_("Failing jobs")}:<ul style="margin-top:6px">{items}</ul></div>'
    else:
        status_html = (f'<div class="algo" style="margin-top:14px">🟢 {_("All enabled jobs report <b>ok</b>.")} '
                       + _("Economy: <b>{n}</b> runs a week (watchdogs, reminders, context logs) are executed by scripts <b>without a single LLM call</b>; precheck jobs wake the model only when there is real work. Tokens are spent by {m} analytical jobs").format(n=f"{noop_week:.0f}", m=len(llm))
                       + (("; " + _("one-off reminders queued — {n}").format(n=len(once_queue))) if once_queue else "") + '.</div>')
    return ('<div class="card full" style="margin-top:15px"><div class="uhead"><h3>' + _("Cron efficiency") + '</h3>'
            f'<span class="pill p-ok">{_("no-op without LLM")}</span></div>' + mrow + mrow2 + status_html + '</div>')


JOB_SQL = """
    -- session id is `cron_<job id>_<timestamp>`; cut between the first and the
    -- second underscore instead of assuming a fixed id length, otherwise every job
    -- whose id is not exactly 12 characters silently renders as «deleted job»
    SELECT substr(id, 6, instr(substr(id, 6), '_') - 1) job, count(*) sess,
           coalesce(sum(input_tokens),0) inp,
           coalesce(sum(output_tokens+reasoning_tokens),0) outr,
           coalesce(sum(cache_read_tokens),0) cache,
           sum(CASE WHEN {paid} THEN 1 ELSE 0 END) paid
    FROM sessions
    WHERE source='cron' AND id LIKE 'cron\\_%' ESCAPE '\\'
      AND started_at >= strftime('%s','now',?)
    GROUP BY job
"""


def job_telemetry_card() -> str:
    if not db_path().is_file():
        return ""
    try:
        raw = json.loads(jobs_path().read_text(encoding="utf-8")).get("jobs", [])
    except (OSError, ValueError):
        raw = []
    names = {j.get("id"): j.get("name") or j.get("id", "?") for j in raw}
    sql = JOB_SQL.format(paid=paid())
    try:
        with sqlite3.connect(f"file:{db_path()}?mode=ro", uri=True) as db:
            db.row_factory = sqlite3.Row
            r30 = {r["job"]: dict(r) for r in db.execute(sql, ("-30 days",))}
            r7 = {r["job"]: dict(r) for r in db.execute(sql, ("-7 days",))}
    except sqlite3.Error:
        return ""
    if not r30:
        return ""
    rows = sorted(r30.values(), key=lambda r: r["inp"], reverse=True)
    tot = sum(r["inp"] for r in rows) or 1
    body = ""
    for r in rows[:12]:
        name = names.get(r["job"])
        label = esc(name[:52] + ("…" if len(name) > 52 else "")) if name else f'<span class="muted">{_("deleted job")} {esc(r["job"])}</span>'
        j7 = r7.get(r["job"], {})
        paid_mark = f' <span title="{_("sessions on paid fallback")}">💳{r["paid"]}</span>' if r["paid"] else ""
        body += (f'<tr><td>{label}{paid_mark}</td><td>{r["sess"]}</td><td>{fmt_tok(j7.get("inp", 0))}</td>'
                 f'<td><b>{fmt_tok(r["inp"])}</b> <span class="muted">{r["inp"] / tot * 100:.0f}%</span></td>'
                 f'<td>{fmt_tok(r["outr"])}</td><td class="muted">{fmt_tok(r["cache"])}</td></tr>')
    rest = rows[12:]
    if rest:
        body += (f'<tr><td class="muted">{_("…{n} more jobs").format(n=len(rest))}</td><td>{sum(r["sess"] for r in rest)}</td><td></td>'
                 f'<td>{fmt_tok(sum(r["inp"] for r in rest))}</td><td>{fmt_tok(sum(r["outr"] for r in rest))}</td>'
                 f'<td class="muted">{fmt_tok(sum(r["cache"] for r in rest))}</td></tr>')
    head = (f'<tr><th>{_("Job")}</th><th>{_("Sessions · 30d")}</th><th>{_("Input · 7d")}</th>'
            f'<th>{_("Input · 30d")}</th><th>{_("Out+reas · 30d")}</th><th>{_("Cache · 30d")}</th></tr>')
    return ('<div class="card full" style="margin-top:15px"><div class="uhead"><h3>' + _("LLM job telemetry · who spends tokens") + '</h3>'
            f'<span class="pill p-avail">{_("state.db · by job id")}</span></div>'
            f'<table class="jtab">{head}{body}</table>'
            f'<div class="algo" style="margin-top:12px">{_("Only LLM cron sessions (script-only jobs spend no tokens and are not here). Input — fresh tokens without cache; cache — context read from the prompt cache; 💳 — sessions that went to a paid fallback. This table says which job to optimise next.")}</div></div>')


def _sched_short(sch: dict) -> str:
    kind = (sch or {}).get("kind")
    if kind == "interval":
        return _("every {n} min").format(n=sch.get("minutes", "?"))
    if kind == "once":
        return _("once")
    if kind == "cron":
        pc = parse_cron(sch.get("expr", ""))
        if pc:
            mn, hr, dom, mon, dow = pc
            return f"{sched_label(dom, mon, dow)} {hr:02d}:{mn:02d}"
        return freq_label(sch.get("expr", ""))
    return ""


def billing_card() -> str:
    cfg = current()
    try:
        raw = json.loads(jobs_path().read_text(encoding="utf-8")).get("jobs", [])
    except (OSError, ValueError):
        return ""
    enabled = [j for j in raw if j.get("enabled", True)]
    free = {(f.get("id"), f.get("model")) for f in cfg.get("providers.free", [])}
    pid = primary_id(cfg)
    groups = {"primary": [], "free": [], "script": [], "paid": []}
    for j in enabled:
        prov, mod = j.get("provider"), j.get("model")
        if j.get("no_agent"):
            key = "script"
        elif (prov, mod) in free or (prov, None) in free:
            key = "free"
        elif prov in (None, "", pid):
            key = "primary"
        else:
            key = "paid"
        groups[key].append((strip_name(j.get("name", "")), _sched_short(j.get("schedule") or {})))

    def subblock(title: str, items, note: str) -> str:
        if not items:
            return ""
        chips = "".join(f'<span class="chip">{esc(nm)} <span class="muted">· {esc(sc)}</span></span>' for nm, sc in sorted(items))
        return (f'<div style="margin-top:13px"><div style="font-weight:600;font-size:13px">{esc(title)} <span class="muted">· {len(items)}</span></div>'
                f'<div class="chips" style="margin-top:5px">{chips}</div><div class="muted" style="font-size:11.5px;margin-top:5px">{esc(note)}</div></div>')

    prim = cfg.get("providers.primary", {})
    sub_usd = float(prim.get("subscription_usd_month", 0) or 0)
    n_paid = len(groups["paid"])
    pill = (f'<span class="pill p-ok">{_("no direct paid jobs")}</span>' if not n_paid
            else f'<span class="pill p-part">{_("{n} on paid API").format(n=n_paid)}</span>')
    blocks = (
        subblock("🧠 " + _("Primary · {p}").format(p=prim.get("label", pid)) + (f" · ${sub_usd:.0f}/" + _("mo") if sub_usd else ""), groups["primary"],
                 _("fixed subscription cost; no per-token billing. If the primary fails the job degrades to the paid fallback — then it is real money."))
        + subblock("🆓 " + _("Free tier"), groups["free"], _("free-tier key — zero per-token cost."))
        + subblock("🆓 " + _("Script-only · no LLM"), groups["script"], _("python scripts, watchdogs and prechecks; no inference — 0 tokens."))
        + subblock("💳 " + _("Direct paid API"), groups["paid"], _("billed per token by default."))
    )
    return ('<div class="card full" style="margin-top:15px"><div class="uhead"><h3>' + _("Cron by API cost") + '</h3>' + pill + '</div>' + blocks
            + f'<div class="algo" style="margin-top:13px">{_("Grouped by billing tier of each job (provider/model fields of jobs.json). Real per-token money is possible only when the primary is down and a job degrades to the paid fallback (those sessions are marked 💳 in the telemetry below).")}</div></div>')


def build() -> str:
    cfg = current()
    jobs, background = load_jobs()
    if not jobs:
        # Only interval jobs (or none the parser understands): there is no day
        # axis to draw, but the economics, billing tiers and per-job telemetry
        # are still real — returning "" here used to erase the whole view.
        rest = efficiency_card() + billing_card() + job_telemetry_card()
        if not (rest.strip() or background):
            return ""
        bg = ('<div class="blk"><h2><em>◆</em> ' + _("Background jobs") + ' <span>'
              + _("no fixed time of day") + '</span></h2><div class="body"><div class="algo">'
              + esc(" · ".join(sorted(background))) + '</div></div></div>') if background else ""
        return sec(_("Day schedule and efficiency"),
                   f'{cfg.get("timezone.name", "UTC")} · jobs.json + crontab · ' + _("run economics"),
                   bg + rest)
    X0, X1 = 310, 790
    LBL, RIGHT, NAME_MAX, ROW_H, TOP = 0, 884, 44, 40, 52
    span = X1 - X0

    def xpos(h, m):
        return X0 + (h + m / 60.0) / 24.0 * span

    jobs.sort(key=lambda j: (j["hour"], j["min"]))
    H = TOP + len(jobs) * ROW_H + 52
    parts = [f'<svg viewBox="0 0 900 {H}" xmlns="http://www.w3.org/2000/svg" role="img" aria-label="{esc(_("Cron schedule: one row per job on the day axis"))}">']
    parts.append(f'<text class="kd" x="{LBL}" y="26">{_("JOB")}</text>')
    parts.append(f'<text class="kd" x="{RIGHT}" y="26" text-anchor="end">{_("MODEL")}</text>')
    body_h = len(jobs) * ROW_H
    parts.append(f'<rect class="band" x="{X0}" y="{TOP-4}" width="{xpos(6,0)-X0:.0f}" height="{body_h}"/>')
    parts.append(f'<rect class="band" x="{xpos(21,0):.0f}" y="{TOP-4}" width="{X1-xpos(21,0):.0f}" height="{body_h}"/>')
    parts.append(f'<line class="grid-l" x1="{X0}" y1="44" x2="{X1}" y2="44" stroke-width="1.4"/>')
    for h in range(0, 25, 3):
        x = xpos(h, 0)
        parts.append(f'<line class="tick" x1="{x:.0f}" y1="38" x2="{x:.0f}" y2="44"/>')
        parts.append(f'<text class="kd" x="{x:.0f}" y="30" text-anchor="middle">{h:02d}</text>')
    for i, j in enumerate(jobs):
        top = TOP + i * ROW_H
        cy = top + 20
        cls = j["cls"]
        x = xpos(j["hour"], j["min"])
        name = j["name"]
        if len(name) > NAME_MAX:
            name = name[:NAME_MAX - 1].rstrip(" ,·-") + "…"
        kind = _("no model") if j.get("no_agent") else (_("by precheck") if j.get("precheck") else _("with a model"))
        parts.append(f'<g class="node-in" style="animation-delay:{.06*i:.2f}s">')
        parts.append(f'<line class="rowline" x1="0" y1="{top}" x2="{RIGHT}" y2="{top}"/>')
        parts.append(f'<line class="guide" x1="{X0}" y1="{cy}" x2="{X1}" y2="{cy}"/>')
        parts.append(f'<text class="tt" x="{LBL}" y="{cy-3}" font-size="12.5">{esc(name)}</text>')
        parts.append(f'<text class="ms" x="{LBL}" y="{cy+13}">{esc(j["dow"])}</text>')
        parts.append(f'<circle class="ring-{cls}" cx="{x:.0f}" cy="{cy}" r="9"/>')
        parts.append(f'<circle class="dot-{cls}" cx="{x:.0f}" cy="{cy}" r="4.5"/>')
        lb = LB_CLASS.get(cls, "lb")
        if x > X0 + span * 0.72:
            parts.append(f'<text class="{lb}" x="{x-15:.0f}" y="{cy+4}" text-anchor="end">{j["hour"]:02d}:{j["min"]:02d}</text>')
        else:
            parts.append(f'<text class="{lb}" x="{x+15:.0f}" y="{cy+4}">{j["hour"]:02d}:{j["min"]:02d}</text>')
        parts.append(f'<text class="ms" x="{RIGHT}" y="{cy+4}" text-anchor="end">{kind}</text>')
        parts.append('</g>')
    parts.append(f'<line class="rowline" x1="0" y1="{TOP + body_h}" x2="{RIGHT}" y2="{TOP + body_h}"/>')
    parts.append(f'<text class="ms" x="0" y="{TOP + body_h + 28}">'
                 + esc(_("Server time · {tz}. Shaded — night. «No model» means the job runs as a script and costs nothing.").format(tz=cfg.get("timezone.name", "UTC")))
                 + '</text></svg>')
    bg_note = (f'<span style="color:var(--ink3)">{_("background")}: ' + esc(" · ".join(sorted(background))) + '</span>') if background else ""
    legend_items = [(str(c.get("class", "lime")), cfg.text(c.get("legend", c.get("key", "")), lang())) for c in _cats()]
    legend_items.append((str(cfg.get("cron.default_category.class", "lime")), cfg.text(cfg.get("cron.default_category.legend", ""), lang())))
    legend_items.append((str(cfg.get("cron.system_category.class", "n")), cfg.text(cfg.get("cron.system_category.legend", ""), lang())))
    css_var = {"lime": "lime", "cy": "cyan", "am": "amber", "n": "neutral"}
    legend = ('<div class="dglg">' + "".join(f'<span><i style="background:var(--{css_var.get(c, c)})"></i>{esc(l)}</span>' for c, l in legend_items if l) + bg_note + '</div>')
    return sec(_("Day schedule and efficiency"), f'{cfg.get("timezone.name", "UTC")} · jobs.json + crontab · ' + _("run economics"),
               f'<div class="blk"><h2><em>◆</em> {_("Day axis")} <span>{_("when what wakes up")}</span></h2>'
               '<div class="body"><div class="dg">' + "".join(parts) + '</div>' + legend + '</div></div>'
               + efficiency_card() + billing_card() + job_telemetry_card())
