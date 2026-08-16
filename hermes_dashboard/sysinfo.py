"""Host facts for the Overview: gateway status, uptime, load, disk, RAM, git,
crontab, autosync log. Everything degrades to "—" — a missing tool never
breaks the page.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .common import home, tz
from .config import current
from .i18n import _


def _run(cmd: list[str], timeout: int = 10) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def gateway_state() -> str:
    unit = str(current().get("paths.gateway_unit", "hermes-gateway"))
    out = _run(["systemctl", "is-active", unit]).strip()
    return out or "unknown"


def uptime_text() -> str:
    """'5 w 2 d' style, from /proc/uptime (locale-independent, translated)."""
    try:
        secs = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return "—"
    d, rem = divmod(int(secs), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    w, d = divmod(d, 7)
    parts = []
    if w:
        parts.append(f"{w} {_('w')}")
    if d:
        parts.append(f"{d} {_('d')}")
    if not w and h:
        parts.append(f"{h} {_('h')}")
    if not w and not d and m:
        parts.append(f"{m} {_('min')}")
    return " ".join(parts) or f"0 {_('min')}"


def loadavg() -> str:
    try:
        return " ".join(Path("/proc/loadavg").read_text().split()[:3])
    except (OSError, IndexError):
        return "—"


def cores() -> str:
    return str(os.cpu_count() or "?")


def disk() -> tuple[str, str]:
    try:
        u = shutil.disk_usage("/")
    except OSError:
        return "—", ""
    gb = 1024 ** 3
    return f"{u.used/gb:.0f}G / {u.total/gb:.0f}G", f"{u.used*100/u.total:.0f}%"


def ram() -> str:
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _s, v = line.partition(":")
            info[k] = int(v.split()[0])
        total = info["MemTotal"] / 1024 / 1024
        used = (info["MemTotal"] - info.get("MemAvailable", 0)) / 1024 / 1024
        return f"{used:.1f} / {total:.1f} GB"
    except (OSError, KeyError, ValueError, IndexError):
        return "—"


def git_head() -> tuple[str, str]:
    """(short sha, 'sha · subject' cut to 50 chars) of the config repo."""
    repo = str(home())
    sha = _run(["git", "-C", repo, "log", "-1", "--format=%h"]).strip()
    subj = _run(["git", "-C", repo, "log", "-1", "--format=%h · %s"]).strip()
    return sha or "—", (subj[:50] if subj else "—")


def crontab_lines() -> list[str]:
    return [l.strip() for l in _run(["crontab", "-l"]).splitlines()
            if l.strip() and not l.strip().startswith("#")]


def cron_hhmm(substr: str) -> str:
    """HH:MM of the first crontab line whose command contains substr, else '—'."""
    for line in crontab_lines():
        parts = line.split(None, 5)
        if len(parts) < 6 or substr not in parts[5]:
            continue
        mn, hr = parts[0], parts[1]
        if re.fullmatch(r"\d{1,2}", mn) and re.fullmatch(r"\d{1,2}", hr):
            return f"{int(hr):02d}:{int(mn):02d}"
    return "—"


def cron_every_minutes(substr: str) -> int | None:
    for line in crontab_lines():
        parts = line.split(None, 5)
        if len(parts) < 6 or substr not in parts[5]:
            continue
        m = re.fullmatch(r"\*/(\d+)", parts[0])
        if m and parts[1] == "*":
            return int(m.group(1))
    return None


def last_sync() -> str:
    """Time of the last autosync run in the dashboard timezone, from git-autosync.log."""
    log = home() / "git-autosync.log"
    if not log.is_file():
        return _("no data")
    ts = None
    rx = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)Z")
    try:
        with log.open(encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if re.search(r"push: ok|commit:|memory:|PR", line):
                    m = rx.match(line)
                    if m:
                        ts = m.group(1)
    except OSError:
        return _("no data")
    if not ts:
        return _("no data")
    try:
        d = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return d.astimezone(tz()).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return ts


def now_label() -> str:
    n = datetime.now(timezone.utc)
    return f"{n.strftime('%Y-%m-%d %H:%M')} UTC · {n.astimezone(tz()).strftime('%H:%M')} {current().get('timezone.label','')}"
