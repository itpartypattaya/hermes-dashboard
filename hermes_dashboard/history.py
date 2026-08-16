"""Dashboard metric history for week-over-week deltas.

Appends one row per day to cache/dashboard-history.csv and returns HTML
delta badges (↑/↓ over ~7 days) for the KPI tiles. The CSV has a header; the
old headerless `date,facts` format is migrated on the fly.
Stdlib only.
"""
from __future__ import annotations

import argparse
import csv
from datetime import date, timedelta
from pathlib import Path

def _hist_path() -> Path:
    from .config import current
    cfg = current()
    return cfg.path_in_home(cfg.get("paths.history_csv", "cache/dashboard-history.csv"))

# column order and formatting type; good_down=True → growth counts as "bad" (spend)
FIELDS = {
    "facts":      {"fmt": "int",  "good_down": False},
    "input7d":    {"fmt": "tok",  "good_down": True},
    "fallback7":  {"fmt": "int",  "good_down": True},
    "sess7":      {"fmt": "int",  "good_down": False},
}
COLS = ["date"] + list(FIELDS)


def _load() -> list[dict]:
    if not _hist_path().is_file():
        return []
    rows: list[dict] = []
    try:
        with _hist_path().open(encoding="utf-8") as fh:
            first = fh.readline()
            fh.seek(0)
            if first.startswith("date,"):
                rows = list(csv.DictReader(fh))
            else:  # legacy headerless date,facts
                for line in fh:
                    parts = line.rstrip("\n").split(",")
                    if len(parts) >= 2 and parts[0]:
                        rows.append({"date": parts[0], "facts": parts[1]})
    except OSError:
        return []
    return rows


def _write(rows: list[dict]) -> None:
    hist = _hist_path()
    hist.parent.mkdir(parents=True, exist_ok=True)
    tmp = hist.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in COLS})
    tmp.replace(hist)


def _update(rows: list[dict], today: str, values: dict) -> list[dict]:
    existing = next((r for r in rows if r.get("date") == today), None)
    if existing is None:
        existing = {"date": today}
        rows.append(existing)
    for k, v in values.items():
        if v is not None and v != "":
            existing[k] = str(v)
    rows.sort(key=lambda r: r.get("date", ""))
    return rows


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _old_value(rows: list[dict], key: str, today: str):
    """Value ~7 days ago: the last row dated ≤ (today-7d)."""
    week_ago = (date.fromisoformat(today) - timedelta(days=7)).isoformat()
    best = None
    for r in rows:
        d = r.get("date", "")
        if d and d <= week_ago and _num(r.get(key)) is not None:
            best = _num(r.get(key))
    return best


def _fmt(kind: str, v: float) -> str:
    if kind == "usd":
        return f"${v:.2f}"
    if kind == "tok":
        av = abs(v)
        if av >= 1_000_000:
            return f"{v/1_000_000:.1f}M"
        if av >= 1_000:
            return f"{v/1_000:.0f}K"
        return f"{v:.0f}"
    return f"{v:.0f}"


def _badge(key: str, today_v: float, old_v: float) -> str:
    delta = today_v - old_v
    meta = FIELDS[key]
    if abs(delta) < 1e-9:
        return '<span class="dlt dlt-0">→ 0</span>'
    up = delta > 0
    # "good" = spend going down OR useful metrics (facts/sess) going up
    good = (not up) if meta["good_down"] else up
    cls = "dlt-good" if good else "dlt-bad"
    arrow = "↑" if up else "↓"
    sign = "+" if up else "−"
    val = _fmt(meta["fmt"], abs(delta))
    return f'<span class="dlt {cls}">{arrow} {sign}{val}</span>'


def update_and_deltas(values: dict) -> dict[str, str]:
    """Record today's values, return {field: badge_html} (empty string while history accumulates)."""
    rows = _load()
    today = date.today().isoformat()
    rows = _update(rows, today, values)
    try:
        _write(rows)
    except OSError:
        pass
    out = {}
    for key in FIELDS:
        tv = _num(values.get(key))
        ov = _old_value(rows, key, today)
        out[key] = _badge(key, tv, ov) if (tv is not None and ov is not None) else ""
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    for f in FIELDS:
        ap.add_argument("--" + f.replace("_", "-"))
    args = ap.parse_args()
    for k, v in update_and_deltas({f: getattr(args, f) for f in FIELDS}).items():
        print(f"DELTA_{k.upper()}={v!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
