#!/usr/bin/env python3
"""Real Anthropic $ → cache/anthropic_cost.json for the dashboard.

Two sources, by priority:

1. **Admin Cost API** — if an organisation admin key is available in the env
   (ANTHROPIC_ADMIN_KEY / HERMES_DASHBOARD_ANTHROPIC_ADMIN_KEY) or in .env.
2. **Cost Report CSV export** — when there is no org account. Download the
   export from the Anthropic console (Cost → Export) and drop it into
   `cache/anthropic_cost_export.csv` (or any `claude_api_cost*.csv` /
   `anthropic_cost*.csv` in cache/ — the newest wins; override the path with
   env HERMES_DASHBOARD_ANTHROPIC_COST_CSV). The settings page uploads it
   there. cost_usd is summed by day → 7d/30d windows; plus `by_type` (what
   really burns money — usually cache writes) and `api_keys` statuses.

`data_through` / `data_age_days` say how far the DATA reaches — `fetched_at`
only says when we last looked, which used to mask a rotten export.

Without either source: silent exit, the dashboard stays on the estimate.
Stdlib only.
"""
from __future__ import annotations

import csv
import json
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HOME = Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))
CACHE = HOME / "cache" / "anthropic_cost.json"
ENDPOINT = "https://api.anthropic.com/v1/organizations/cost_report"


# ─────────────────────── source 1: Admin Cost API ────────────────────────

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


def _admin_key() -> str:
    for name in ("HERMES_DASHBOARD_ANTHROPIC_ADMIN_KEY", "ANTHROPIC_ADMIN_KEY"):
        v = os.environ.get(name, "").strip()
        if v:
            return v
    # fallback: read from .env instead of relying on the environment export
    envf = HOME / ".env"
    if envf.is_file():
        try:
            for line in envf.read_text(encoding="utf-8", errors="ignore").splitlines():
                for name in ("HERMES_DASHBOARD_ANTHROPIC_ADMIN_KEY", "ANTHROPIC_ADMIN_KEY"):
                    if line.startswith(name + "="):
                        return line.split("=", 1)[1].strip()
        except OSError:
            pass
    return ""


# Daily buckets are capped at 31 per page by the API (default 7). Asking for
# more is a 400, and a swallowed 400 looks exactly like "no data" — the cache
# silently stays on an old CSV. Hence: legal page size + follow next_page.
MAX_DAILY_BUCKETS = 31


def _error_message(e) -> str:
    """The provider's own error text, without echoing the raw body.

    The message is the whole point of logging a failure ("limit: 32 is greater
    than the maximum of 31" is the difference between a fixed bug and a silent
    one), but a raw body is an unbounded string from the network that ends up
    in a log someone may paste. So: parse the documented error envelope and
    print only its message, truncated; anything else becomes a shrug.
    """
    try:
        payload = json.loads(e.read().decode("utf-8", "replace"))
        msg = (payload.get("error") or {}).get("message")
        return str(msg)[:200] if msg else "(no message in the error body)"
    except Exception:
        return "(unparseable error body)"


def _fetch_api(key: str) -> list[dict] | None:
    """All daily cost buckets of the last 30 days, or None (fail-open).

    None means "we could not ask" and leaves the previous cache alone; the
    reason goes to stderr, because a collector that fails in silence is
    indistinguishable from a provider that spent nothing.
    """
    start = (datetime.now(timezone.utc) - timedelta(days=30)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    base = (ENDPOINT + "?starting_at=" + start.strftime("%Y-%m-%dT%H:%M:%SZ")
            + "&bucket_width=1d&limit=" + str(MAX_DAILY_BUCKETS))
    out: list[dict] = []
    page = None
    for _hop in range(12):          # bounded: 30 days / 31 per page is one hop
        url = base + ("&page=" + urllib.parse.quote(page) if page else "")
        req = urllib.request.Request(url, headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "accept": "application/json",
            "user-agent": "hermes-dashboard/1.0 (+https://github.com/itpartypattaya/hermes-dashboard)",
        })
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            print("[anthropic_cost] cost_report HTTP %s: %s" % (e.code, _error_message(e)),
                  file=sys.stderr)
            return None
        except (urllib.error.URLError, ValueError, OSError) as e:
            print("[anthropic_cost] cost_report unreachable: %s" % (e,), file=sys.stderr)
            return None
        out.extend(payload.get("data") or [])
        if not payload.get("has_more"):
            return out
        page = payload.get("next_page")
        if not page:
            return out
    print("[anthropic_cost] cost_report: too many pages, truncated", file=sys.stderr)
    return out


def _from_api(key: str) -> dict | None:
    data = _fetch_api(key)
    if data is None:
        return None  # fail-open: do not overwrite the previous cache
    per_day: dict[str, float] = defaultdict(float)
    for bucket in data:
        cents = sum(float(r.get("amount") or 0) for r in bucket.get("results", []))
        day = str(bucket.get("starting_at", ""))[:10]
        if day:
            per_day[day] += cents / 100.0
    return _summarize(per_day, source="admin_api")


# ─────────────────────── source 2: CSV Cost Report ───────────────────────
def _csv_source() -> Path | None:
    explicit = os.environ.get("HERMES_DASHBOARD_ANTHROPIC_COST_CSV", "").strip()
    if explicit and Path(explicit).is_file():
        return Path(explicit)
    cache_dir = HOME / "cache"
    if not cache_dir.is_dir():
        return None
    seen: dict[Path, float] = {}
    for pat in ("anthropic_cost_export.csv", "claude_api_cost*.csv", "anthropic_cost*.csv"):
        for f in cache_dir.glob(pat):
            if f.name == CACHE.name:  # skip our own JSON cache
                continue
            if f.is_file():
                seen[f] = f.stat().st_mtime
    if not seen:
        return None
    return max(seen, key=seen.get)


def _from_csv(path: Path) -> dict | None:
    per_day: dict[str, float] = defaultdict(float)
    per_day_type: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    api_keys: dict[str, dict] = {}
    try:
        with path.open(encoding="utf-8-sig", errors="ignore", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                day = (row.get("usage_date_utc") or "").strip()[:10]
                if not day:
                    continue
                try:
                    cost = float(row.get("cost_usd") or 0)
                except (TypeError, ValueError):
                    continue
                per_day[day] += cost
                ttype = (row.get("token_type") or "").strip()
                if ttype:
                    per_day_type[day][ttype] += cost
                # api_key_id/api_key_status columns are not present in every export
                kid = (row.get("api_key_id") or "").strip()
                if kid:
                    api_keys[kid] = {
                        "id": kid,
                        "name": (row.get("api_key") or "").strip(),
                        "status": (row.get("api_key_status") or "").strip(),
                    }
    except OSError:
        return None
    if not per_day:
        return None
    out = _summarize(per_day, source="csv", per_day_type=per_day_type)
    out["csv_file"] = path.name
    if api_keys:
        out["api_keys"] = sorted(api_keys.values(), key=lambda k: k["id"])
    return out


# ─────────────────────────── shared window roll-up ────────────────────────────
def _is_iso_date(s: str) -> bool:
    try:
        date.fromisoformat(s)
        return True
    except ValueError:
        return False


def _summarize(per_day: dict[str, float], *, source: str,
               per_day_type: dict[str, dict[str, float]] | None = None) -> dict:
    today = datetime.now(timezone.utc).date()
    usd_30d = 0.0
    usd_7d = 0.0
    buckets = []
    by_type: dict[str, float] = defaultdict(float)
    for day in sorted(per_day):
        usd = per_day[day]
        try:
            d = date.fromisoformat(day)
        except ValueError:
            continue
        age = (today - d).days
        if age < 0 or age > 29:
            continue
        usd_30d += usd
        if age <= 6:
            usd_7d += usd
        buckets.append({"date": day, "usd": round(usd, 4)})
        # "what the bill is made of" breakdown — same 30d window as usd_30d
        if per_day_type:
            for t, v in per_day_type.get(day, {}).items():
                by_type[t] += v
    # How far the source data actually REACHES. Without this field the dashboard
    # could not tell "spent $0 in the last 7 days" from "the export ended 20 days
    # ago, we know nothing about the last 7 days" — and showed the latter as the
    # former (a stale CSV hid a whole week of paid-model usage). fetched_at does
    # not help here: it refreshes on every run and makes stale data look fresh.
    days = [d for d in (date.fromisoformat(x) for x in sorted(per_day)
                        if _is_iso_date(x))]
    data_through = max(days).isoformat() if days else None
    out = {
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "currency": "USD",
        "source": source,
        "usd_30d": round(usd_30d, 2),
        "usd_7d": round(usd_7d, 2),
        "data_through": data_through,
        "data_age_days": (today - max(days)).days if days else None,
        "buckets": buckets,
    }
    if by_type:
        out["by_type"] = {t: round(v, 4) for t, v in
                          sorted(by_type.items(), key=lambda kv: -kv[1])}
    return out


def main() -> int:
    out: dict | None = None
    key = _admin_key()
    if key:
        out = _from_api(key)
    if out is None:
        csv_path = _csv_source()
        if csv_path is not None:
            out = _from_csv(csv_path)
    if out is None:
        return 0  # no source — the dashboard stays on the token-based estimate

    _atomic_json(CACHE, out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
