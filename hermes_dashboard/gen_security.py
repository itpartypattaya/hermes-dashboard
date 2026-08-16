"""«Security and shield» — only what is measurable.

  • Tirith fail-open events over 7 days — grep of the logs with the same regex the
    watchdog script uses (security.tirith_regex); 0 = the engine never failed open.
  • Watchdog statuses (security/cron.watchdogs) — from cron/jobs.json.
  • Shield file — last git change date; Tirith flags from config.yaml.
  • Prompt-injection evals — number of scenarios in security.evals_path.
If a detector does not exist for something, the metric is not drawn at all —
never an eternal zero.
"""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timedelta

from .common import esc, home, jobs_path, kpi, sec, tz, yaml_get
from .config import current
from .i18n import _, lang


def tirith_failures_7d() -> int | None:
    cfg = current()
    rx_s = str(cfg.get("security.tirith_regex", "") or "")
    if not rx_s:
        return None
    rx = re.compile(rx_s, re.IGNORECASE)
    cutoff = datetime.now() - timedelta(days=7)
    line_re = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)")
    n = 0
    seen = False
    for name in cfg.get("security.log_files", []):
        p = home() / name
        if not p.is_file():
            continue
        seen = True
        try:
            with p.open(encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if not rx.search(line):
                        continue
                    m = line_re.match(line)
                    if m:
                        try:
                            if datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S") < cutoff:
                                continue
                        except ValueError:
                            pass
                    n += 1
        except OSError:
            continue
    return n if seen else None


def watchdog_state(jid: str) -> tuple[str, str]:
    try:
        jobs = json.loads(jobs_path().read_text(encoding="utf-8")).get("jobs", [])
    except (OSError, ValueError):
        return "—", "—"
    j = next((x for x in jobs if x.get("id") == jid), None)
    if not j:
        return _("no job"), "—"
    if not j.get("enabled", True):
        return _("disabled"), "—"
    st = j.get("last_status") or "—"
    try:
        tick = datetime.fromisoformat(j.get("last_run_at") or "").astimezone(tz()).strftime("%d.%m %H:%M")
    except (ValueError, TypeError):
        tick = "—"
    return st, tick


def file_git_date(rel: str) -> str:
    try:
        out = subprocess.run(["git", "-C", str(home()), "log", "-1", "--format=%cs", "--", rel],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        if out:
            return out
    except (OSError, subprocess.SubprocessError):
        pass
    p = home() / rel
    return datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d") if p.is_file() else "—"


def evals_count(rel: str) -> int:
    try:
        d = json.loads((home() / rel).read_text(encoding="utf-8"))
        return len(d.get("evals", [])) if isinstance(d, dict) else len(d)
    except (OSError, ValueError, TypeError):
        return 0


def build() -> str:
    cfg = current()
    tiles = ""
    tf = tirith_failures_7d()
    chan = str(cfg.get("security.alert_channel_label", "") or "")
    if tf is not None:
        tiles += kpi("ok" if tf == 0 else "no", _("Tirith fail-open · 7d"),
                     (_("0 · clean") if tf == 0 else str(tf)),
                     _("command scanner failures") if tf == 0 else (_("engine failed open") + (f" — {chan}" if chan else "")))
    for w in cfg.get("cron.watchdogs", []):
        st, tick = watchdog_state(str(w.get("id", "")))
        cls = "ok" if st == "ok" else ("warn" if st in ("—", _("no job")) else "no")
        tiles += kpi(cls, cfg.text(w.get("label", ""), lang()), esc(st),
                     f"{cfg.text(w.get('schedule', ''), lang())} · {_('tick')} {tick}")

    cards = ""
    shield = str(cfg.get("security.shield_file", "") or "")
    en = yaml_get("tirith_enabled", "")
    fo = yaml_get("tirith_fail_open", "")
    bullets = cfg.list_text(cfg.get("security.shield_bullets", {}), lang())
    if shield or bullets or en:
        lis = "".join(f"<li>{b}</li>" for b in bullets)
        if en:
            fo_note = (_("fail-open: when Tirith fails, commands are not blocked — that is why a watchdog watches the engine")
                       if fo == "true" else _("fail-closed: a Tirith failure blocks commands"))
            lis += f"<li>{_('Tirith command scanner')}: enabled=<b>{esc(en)}</b> · {esc(fo_note)}</li>"
        pill = (f'<span class="pill p-ok">{_("updated")} {esc(file_git_date(shield))}</span>' if shield else "")
        cards += (f'<div class="card"><div class="uhead"><h3>{_("Shield — rules of the agent")}</h3>{pill}</div>'
                  f'<ul>{lis}</ul></div>')
    evals = str(cfg.get("security.evals_path", "") or "")
    ebul = cfg.list_text(cfg.get("security.evals_bullets", {}), lang())
    if evals:
        n = evals_count(evals)
        cards += (f'<div class="card"><div class="uhead"><h3>{_("Prompt-injection evals")}</h3>'
                  f'<span class="pill p-avail">{n} {_("scenarios")}</span></div><ul>'
                  + "".join(f"<li>{b}</li>" for b in ebul) + '</ul></div>')

    if not tiles and not cards:
        return ""
    body = (f'<div class="kpis">{tiles}</div>' if tiles else "") + \
           (f'<div class="grid us2" style="margin-top:15px">{cards}</div>' if cards else "")
    return sec(_("Security and shield"), _("watchdogs · logs 7d · rules · evals"), body, sec_id="security")
