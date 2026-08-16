"""«Important events» — a curated feed, not a raw log.

Sources (all already exist, nothing invented):
  • cron/jobs.json — jobs with last_status != ok (red) + the last dreaming ok (green);
  • state.db      — forced fallback sessions over 7 days (amber: primary was down);
  • git-autosync.log — last sync run (blue);
  • git (config repo) — last deploy/commit (blue).
Sorted by time, max 8 rows; without incidents the first row is a green "no incidents".
"""
from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from datetime import datetime, timezone

from .common import connect_ro, esc, fallback, home, jobs_path, paid_label, tz, tz_label
from .config import current
from .i18n import _

MAX_EVENTS = 8


def ev(epoch: float, color: str, html: str) -> dict:
    return {"t": epoch, "color": color, "html": html}


def cron_events() -> list[dict]:
    out: list[dict] = []
    dream_id = str(current().get("cron.dream_job_id", "") or "")
    try:
        jobs = json.loads(jobs_path().read_text(encoding="utf-8")).get("jobs", [])
    except (OSError, ValueError):
        return out
    for j in jobs:
        if not j.get("enabled", True):
            continue
        st, ts = j.get("last_status"), j.get("last_run_at")
        if not st or not ts:
            continue
        try:
            epoch = datetime.fromisoformat(ts).timestamp()
        except (ValueError, TypeError):
            continue
        name = j.get("name", j.get("id", "?"))
        if st != "ok":
            err = str(j.get("last_error") or "")[:120]
            out.append(ev(epoch, "var(--no)",
                          _("Cron <b>{n}</b> finished with status <b>{s}</b>").format(n=esc(name), s=esc(st))
                          + ((" — " + esc(err)) if err else "")))
        elif dream_id and j.get("id") == dream_id:
            out.append(ev(epoch, "var(--ok)", _("Nightly memory consolidation (<b>dreaming</b>) — ok")))
    return out


def fallback_events() -> list[dict]:
    out: list[dict] = []
    db = connect_ro()
    if db is None:
        return out
    try:
        with db:
            rows = db.execute(
                "SELECT started_at, billing_provider, model FROM sessions WHERE " + fallback() +
                " AND started_at >= strftime('%s','now','-7 days') ORDER BY started_at DESC LIMIT 4"
            ).fetchall()
    except sqlite3.Error:
        return out
    finally:
        db.close()
    prim = current().get("providers.primary.label", "primary")
    for started, prov, model in rows:
        out.append(ev(float(started), "var(--part)",
                      _("{p} was unavailable — a session went to the paid fallback <b>{f}</b>").format(
                          p=esc(prim), f=esc(paid_label(prov, model)))))
    return out


def autosync_event() -> list[dict]:
    log = home() / "git-autosync.log"
    if not log.is_file():
        return []
    ts_re = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)Z?\s+(.*)")
    last = None
    try:
        with log.open(encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                m = ts_re.match(line)
                if m and re.search(r"push: ok|commit:|PR|no-op|memory:", m.group(2)):
                    last = m
    except OSError:
        return []
    if not last:
        return []
    try:
        epoch = datetime.strptime(last.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return []
    return [ev(epoch, "var(--avail)", _("Config autosync: <b>{m}</b>").format(m=esc(last.group(2).strip()[:110])))]


def deploy_event() -> list[dict]:
    try:
        out = subprocess.run(["git", "-C", str(home()), "log", "-1", "--format=%ct\t%s"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        epoch_s, subj = out.split("\t", 1)
        return [ev(float(epoch_s), "var(--avail)", _("Config deploy: <b>{s}</b>").format(s=esc(subj[:90])))]
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def build() -> str:
    events = cron_events() + fallback_events() + autosync_event() + deploy_event()
    if not events:
        return ""
    events.sort(key=lambda e: e["t"], reverse=True)
    events = events[:MAX_EVENTS]
    has_incident = any(e["color"] in ("var(--no)", "var(--part)") for e in events)
    rows = ""
    if not has_incident:
        rows += ('<div class="evt"><span class="edot" style="background:var(--ok)"></span>'
                 f'<span class="ets">{_("now")}</span>'
                 f'<span class="etx"><b>{_("No incidents")}</b> — {_("cron, sync and providers are nominal")}</span></div>')
    for e in events:
        when = datetime.fromtimestamp(e["t"], tz()).strftime("%d.%m %H:%M")
        rows += (f'<div class="evt"><span class="edot" style="background:{e["color"]}"></span>'
                 f'<span class="ets">{when}</span><span class="etx">{e["html"]}</span></div>')
    return (
        f'<div class="sec" id="events"><div class="sec-h"><h2>{_("Important events")}</h2>'
        f'<span class="ln"></span><span class="note">{_("cron · fallback · sync · time")} {esc(tz_label())}</span></div>'
        f'<div class="card full">{rows}'
        f'<div class="algo">{_("Curated feed: cron failures and forced paid fallbacks are incidents; sync and deploy are the config life-cycle. Full logs live on the server.")}</div>'
        '</div></div>'
    )
