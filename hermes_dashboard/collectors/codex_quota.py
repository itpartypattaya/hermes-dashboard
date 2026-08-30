#!/usr/bin/env python3
"""Subscription quota helper for the primary provider (Codex/ChatGPT plan).

Pulls a live snapshot of the account limits through the Hermes core
`account_usage` API (the same endpoint the status bar uses):

  --write-cache  (default) — writes cache/codex_quota.json for the dashboard.
      Fail-open: on a network error the old cache is left untouched.
  --alert        — prints a warning ONLY when the remainder of the "long" window
      is below the threshold (env HERMES_DASHBOARD_QUOTA_ALERT_PCT, default 15),
      otherwise {"wakeAgent": false} so a no_agent cron stays silent.
      Which window is guarded — see pick_guard_window: never look for 'weekly'
      strictly, upstream does not always return it.

Run with the Hermes venv python (needs httpx + hermes-agent on sys.path):
  <venv>/bin/python -m hermes_dashboard.collectors.codex_quota --write-cache
Env: HERMES_HOME, HERMES_DASHBOARD_PROVIDER (default openai-codex),
HERMES_DASHBOARD_TZ_OFFSET_H (default 0) for the human reset label.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
CACHE = HOME / "cache" / "codex_quota.json"
TZ = timezone(timedelta(hours=int(os.environ.get("HERMES_DASHBOARD_TZ_OFFSET_H", "0"))))
PROVIDER = os.environ.get("HERMES_DASHBOARD_PROVIDER", "openai-codex")

# Window key → label. We do NOT name the window duration: upstream turned out to
# return a single 'Session' window that resets after a WEEK, while the card labelled
# it "5 hours" — the label was a guess, not data. The dashboard shows the duration
# computed from reset_at (reset_in_h) instead.
WINDOW_LABELS = {"session": "session window", "weekly": "weekly window"}



def _atomic_json(target: Path, payload) -> None:
    """Write the cache in one step; a build may be reading it right now.

    Local rather than shared because a collector is spawned as its own process
    under the agent's venv python and must import as little as possible.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix="." + target.name + ".",
                                    suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, indent=2))
        tmp.replace(target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _fetch_snapshot():
    """Live snapshot of Codex limits, or None (fail-open)."""
    sys.path.insert(0, str(HOME / "hermes-agent"))
    try:
        from agent.account_usage import fetch_account_usage
    except Exception:
        return None
    try:
        return fetch_account_usage(PROVIDER)
    except Exception:
        return None


def _snapshot_to_dict(snap) -> dict | None:
    if snap is None or not getattr(snap, "windows", None):
        return None
    out = {
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "plan": snap.plan,
        "windows": [],
    }
    now = datetime.now(timezone.utc)
    for w in snap.windows:
        if w.used_percent is None:
            continue
        # Session → session, Weekly → weekly (robust to case/language)
        key = "session" if "session" in w.label.lower() else (
            "weekly" if "week" in w.label.lower() else w.label.lower())
        used = max(0.0, min(100.0, float(w.used_percent)))
        reset_iso = w.reset_at.astimezone(timezone.utc).isoformat() if w.reset_at else None
        reset_local = w.reset_at.astimezone(TZ).strftime("%d.%m %H:%M") if w.reset_at else None
        # time until reset is the only honest indicator of window "length":
        # the dashboard computes it instead of an invented label
        reset_in_h = round((w.reset_at - now).total_seconds() / 3600.0, 1) if w.reset_at else None
        out["windows"].append({
            "key": key,
            "label": WINDOW_LABELS.get(key, w.label),
            "upstream_label": w.label,   # what upstream actually sent (diagnostics)
            "used_percent": round(used, 1),
            "remaining_percent": round(100.0 - used, 1),
            "reset_at": reset_iso,
            "reset_local": reset_local,
            "reset_in_h": reset_in_h,
        })
    return out if out["windows"] else None


def pick_guard_window(windows: list) -> dict | None:
    """The window the alert watches: the one that will NOT reset for the longest.

    The alert used to look strictly for key == 'weekly'. It turned out upstream
    returns a single 'Session' window (weekly reset) — there is no 'weekly' at
    all, so the alert silently returned "no need to wake" on EVERY run (every 6
    hours, last_status=ok). That is exactly why no warning preceded a quota
    lockout.

    Now: take weekly if present; otherwise the window with the farthest reset
    (that is the one the quota actually runs out on). With no reset_at at all —
    the window with the smallest remainder, so the alert is not lost in that case
    either.
    """
    windows = [w for w in (windows or []) if w.get("remaining_percent") is not None]
    if not windows:
        return None
    weekly = next((w for w in windows if w.get("key") == "weekly"), None)
    if weekly:
        return weekly
    dated = [w for w in windows if w.get("reset_in_h") is not None]
    if dated:
        return max(dated, key=lambda w: w["reset_in_h"])
    return min(windows, key=lambda w: w["remaining_percent"])


def _write_cache() -> int:
    data = _snapshot_to_dict(_fetch_snapshot())
    if data is None:
        # fail-open: do not overwrite the previous cache with a fresh failure
        return 0
    _atomic_json(CACHE, data)
    return 0


def _alert() -> int:
    threshold = float(os.environ.get("HERMES_DASHBOARD_QUOTA_ALERT_PCT", "15"))
    data = _snapshot_to_dict(_fetch_snapshot())
    if data is None:
        print('{"wakeAgent": false}')       # transient error — do not wake
        return 0
    guard = pick_guard_window(data["windows"])
    if guard is None or guard["remaining_percent"] > threshold:
        print('{"wakeAgent": false}')
        return 0
    reset = guard.get("reset_local") or "?"
    label = guard.get("label") or guard.get("key") or "quota"
    print(
        f"⚠️ Subscription quota almost exhausted ({label}): {guard['remaining_percent']:.0f}% left. "
        f"Resets {reset}. Answers will soon go to the paid fallback — consider pausing heavy tasks."
    )
    return 0


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "--write-cache"
    if mode == "--alert":
        return _alert()
    return _write_cache()


if __name__ == "__main__":
    raise SystemExit(main())
