"""Shared layer for all generators: paths, session classification (SQL fragments
built from dashboard.json), formatting, HTML bricks, config.yaml access.

Session classification (fragments for `WHERE` on `sessions`):
  active()   — the model was actually called (api_call_count > 0). Telegram groups
               create a session per observed thread with zero model calls; counting
               those as "agent sessions" inflates activity by a quarter.
  paid()     — session on a usage-billed provider (dashboard.json providers.paid;
               a provider id may be narrowed by model_like/exclude_models when the
               same id carries a free tier). Model checks are POSITIVE ("model is
               gemini-*"): older cron fallbacks switched provider but kept the primary
               model name, and "gemini + gpt-x" must not count as paid.
  fallback() — paid AND source in providers.fallback_sources: a *forced* fallback.
               CLI runs on a paid key are deliberate, not an incident.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from datetime import timedelta, timezone
from pathlib import Path

from .config import Config, current
from .i18n import _


def read_text_safe(path: Path, default: str = "") -> str:
    """Read a file the agent wrote, surviving whatever is actually in it.

    `read_text(encoding="utf-8")` raises UnicodeDecodeError on a single bad
    byte — and UnicodeDecodeError is a ValueError, so every `except OSError`
    around these reads let it through. A killed writer leaves a truncated
    multibyte sequence in exactly the files we read (memories, logs, config),
    and the result was a build that produced no pages at all.

    A replacement character in one label is a far better outcome than a
    dashboard frozen on yesterday, so decoding is lenient; a missing or
    unreadable file still returns `default`.
    """
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return default


def atomic_write(target: Path, data, mode: int = 0o644) -> None:
    """Publish a file in one step, safely against a concurrent writer.

    Every writer here can run twice at once — cron and a manual rebuild, the
    build and the settings server. A temp name derived from the target (the old
    `<name>.tmp`) means both writers use the same path: one loses its rename,
    or a reader sees bytes from both. The temp name is therefore unique per
    call, and lands in the target's own directory so the rename stays on one
    filesystem (a cross-device rename is not atomic).

    `data` may be str or bytes. The rename retries briefly because Windows
    refuses to replace a file while a reader holds it; POSIX does not care.
    """
    binary = isinstance(data, (bytes, bytearray))
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix="." + target.name + ".",
                                    suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        if binary:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
        else:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                fh.write(data)
        os.chmod(tmp, mode)
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


def home() -> Path:
    return current().home


def db_path() -> Path:
    return current().home / "state.db"


def jobs_path() -> Path:
    return current().home / "cron" / "jobs.json"


def cache_dir() -> Path:
    return current().home / "cache"


def tz() -> timezone:
    """The configured UTC offset, or UTC if it is unusable.

    validate() rejects a bad offset when the form saves it, but dashboard.json
    is a file the README tells people to edit by hand, so the read path has to
    survive what the write path never let through. datetime rejects |offset|
    >= 24h; anything non-numeric never reaches datetime at all.
    """
    raw = current().get("timezone.offset_hours", 0)
    try:
        hours = int(raw)
    except (TypeError, ValueError):
        hours = 0
    if not -23 <= hours <= 23:
        hours = 0
    return timezone(timedelta(hours=hours))


def tz_label() -> str:
    return str(current().get("timezone.label", "UTC"))


# ── SQL classification ───────────────────────────────────────────────

def _q(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


# Public name for the same thing: every SQL literal built from config goes
# through it, so no caller has to remember to double an apostrophe.
sql_str = _q


def active() -> str:
    return "coalesce(api_call_count,0) > 0"


def provider_cond(p: dict) -> str:
    """WHERE fragment for one providers.paid entry."""
    parts = [f"billing_provider={_q(p['id'])}"]
    if p.get("model_like"):
        parts.append(f"coalesce(model,'') LIKE {_q(p['model_like'])}")
    for m in p.get("exclude_models") or []:
        parts.append(f"coalesce(model,'') <> {_q(m)}")
    return "(" + " AND ".join(parts) + ")"


def paid(cfg: Config | None = None) -> str:
    cfg = cfg or current()
    conds = [provider_cond(p) for p in cfg.get("providers.paid", []) if p.get("id")]
    return "(" + " OR ".join(conds) + ")" if conds else "(0)"


def free_cond(cfg: Config | None = None) -> str:
    cfg = cfg or current()
    conds = []
    for f in cfg.get("providers.free", []):
        if not f.get("id"):
            continue
        c = f"billing_provider={_q(f['id'])}"
        if f.get("model"):
            c += f" AND coalesce(model,'')={_q(f['model'])}"
        conds.append("(" + c + ")")
    return "(" + " OR ".join(conds) + ")" if conds else "(0)"


def fallback(cfg: Config | None = None) -> str:
    """A *forced* fallback: paid, actually called, from an interactive source."""
    cfg = cfg or current()
    srcs = cfg.get("providers.fallback_sources", []) or []
    if not srcs:
        return "(" + paid(cfg) + " AND " + active() + ")"
    return ("(" + paid(cfg) + " AND " + active()
            + " AND source IN (" + ",".join(_q(s) for s in srcs) + "))")


def deliberate_paid(cfg: Config | None = None) -> str:
    """Paid work that nobody was forced into — a run started outside the
    interactive sources. Complementary to fallback(), not its NOT()."""
    cfg = cfg or current()
    srcs = cfg.get("providers.fallback_sources", []) or []
    c = "(" + paid(cfg) + " AND " + active()
    if srcs:
        c += " AND coalesce(source,'') NOT IN (" + ",".join(_q(s) for s in srcs) + ")"
    return c + ")"


def primary_id(cfg: Config | None = None) -> str:
    return str((cfg or current()).get("providers.primary.id", ""))


def is_paid_row(prov: str, model: str, cfg: Config | None = None) -> bool:
    """Python twin of paid() for single rows (banner, chain state).

    Must reach the same verdict as the SQL, or the routing banner and the cost
    card contradict each other on the same page. Two traps live here:

    * SQLite `LIKE` is case-insensitive for ASCII, while `fnmatch.fnmatch`
      folds case only where the *filesystem* does — so this used to agree on
      Windows and disagree on Linux, which is where it actually runs.
      `fnmatchcase` on lowered strings makes the rule explicit instead of
      platform-dependent.
    * `<>` in SQL is case-sensitive, so exclude_models stays an exact match.
    """
    import fnmatch
    cfg = cfg or current()
    model = model or ""
    for p in cfg.get("providers.paid", []):
        if prov != p.get("id"):
            continue
        ml = p.get("model_like")
        if ml and not fnmatch.fnmatchcase(model.lower(), str(ml).replace("%", "*").lower()):
            continue
        if model in (p.get("exclude_models") or []):   # SQL <> is case-sensitive
            continue
        return True
    return False


def paid_label(prov: str, model: str = "", cfg: Config | None = None) -> str:
    cfg = cfg or current()
    for p in cfg.get("providers.paid", []):
        if prov == p.get("id"):
            return str(p.get("label") or prov)
    return model or prov


# ── DB helpers ────────────────────────────────────────────────────────

def connect_ro():
    p = db_path()
    if not p.is_file():
        return None
    db = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def scalar(sql: str, args=(), default=None):
    db = connect_ro()
    if db is None:
        return default
    try:
        with db:
            row = db.execute(sql, args).fetchone()
        return row[0] if row and row[0] is not None else default
    except sqlite3.Error:
        return default
    finally:
        db.close()


def rows(sql: str, args=()) -> list:
    db = connect_ro()
    if db is None:
        return []
    try:
        with db:
            return db.execute(sql, args).fetchall()
    except sqlite3.Error:
        return []
    finally:
        db.close()


# ── formatting ────────────────────────────────────────────────────────

ESC = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}


def esc(s) -> str:
    return "".join(ESC.get(c, c) for c in str(s or ""))


def fmt_tok(n) -> str:
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.0f}K"
    return str(n)


def fmt_usd(x: float) -> str:
    return f"${x:.0f}" if x >= 100 else f"${x:.2f}"


def num(n) -> str:
    return f"{int(n):,}".replace(",", " ")


# ── HTML bricks ───────────────────────────────────────────────────────

def metrics(pairs) -> str:
    cells = "".join(
        f'<div class="um"><div class="uml">{esc(l)}</div><div class="umv">{esc(v)}</div></div>'
        for l, v in pairs
    )
    return f'<div class="umrow">{cells}</div>'


def simple_bar(label: str, value, vmax, color: str, value_txt: str | None = None) -> str:
    w = max(2, int(value * 100 / vmax)) if vmax else 2
    return (
        f'<div class="bar"><span class="blab">{esc(label)}</span>'
        f'<span class="btrack"><span class="bfill" style="width:{w}%;background:{color}"></span></span>'
        f'<span class="bval">{esc(value_txt if value_txt is not None else value)}</span></div>'
    )


def kpi(cls: str, label: str, value: str, sub: str, size: int = 19) -> str:
    return (f'<div class="kpi {cls}"><div class="l">{esc(label)}</div>'
            f'<div class="v" style="font-size:{size}px">{value}</div>'
            f'<div class="x">{esc(sub)}</div></div>')


def sec(title: str, note: str, body: str, sec_id: str = "") -> str:
    idattr = f' id="{sec_id}"' if sec_id else ""
    return (f'<div class="sec"{idattr}><div class="sec-h"><h2>{esc(title)}</h2>'
            f'<span class="ln"></span><span class="note">{esc(note)}</span></div>{body}</div>')


def ubar(label: str, cur: int, limit: int, sub: str) -> str:
    pct = (cur / limit * 100) if limit else 0
    w = max(2.0, min(100.0, pct))
    color = "var(--no)" if pct >= 100 else ("var(--part)" if pct >= 85 else "var(--ok)")
    return (
        f'<div class="ubar"><div class="ubh"><span>{esc(label)}</span>'
        f'<b>{fmt_tok(cur)} / {fmt_tok(limit)}</b></div>'
        f'<div class="ubt"><span class="ubf" style="width:{w:.0f}%;background:{color}"></span></div>'
        f'<div class="ubp">{pct:.0f}% {esc(sub)}</div></div>'
    )


def eff_bar(label: str, pct: float, sub: str, color: str) -> str:
    w = max(2.0, min(100.0, pct))
    return (
        f'<div class="ubar"><div class="ubh"><span>{esc(label)}</span><b>{pct:.0f}%</b></div>'
        f'<div class="ubt"><span class="ubf" style="width:{w:.0f}%;background:{color}"></span></div>'
        f'<div class="ubp">{esc(sub)}</div></div>'
    )


def remain_color(remaining: float) -> str:
    if remaining <= 10:
        return "var(--no)"
    if remaining <= 25:
        return "var(--part)"
    return "var(--ok)"


# ── config.yaml without PyYAML ───────────────────────────────────────

def yaml_get(path: str, default: str = "") -> str:
    """Scalar at dotted path from config.yaml by naive indentation walk.

    Enough for flat provider keys (stt.provider, auxiliary.vision.model); the
    system python has no PyYAML and generators must stay stdlib-only.
    """
    lines = read_text_safe(home() / "config.yaml").splitlines()
    if not lines:
        return default
    keys = path.split(".")
    depth = 0
    want_indent = 0
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent < want_indent:
            if depth:
                return default
            continue
        if indent != want_indent:
            continue
        key, sep, val = line.strip().partition(":")
        if not sep or key != keys[depth]:
            continue
        val = val.split(" #", 1)[0].strip().strip("'\"")
        if depth == len(keys) - 1:
            return val or default
        depth += 1
        want_indent = indent + 2
    return default


def env_key_names() -> set[str]:
    """NAMES of variables in .env (values are never read or printed): used only to
    say "key X is configured" on the providers card."""
    names: set[str] = set()
    try:
        for line in read_text_safe(home() / ".env").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            names.add(line.split("=", 1)[0].strip().removeprefix("export "))
    except OSError:
        pass
    return names


def read_json(path: Path):
    try:
        import json
        return json.loads(read_text_safe(path, "null"))
    except (OSError, ValueError):
        return None


def label_for(value: str, table: dict, fallback: str = "") -> str:
    return _(str(table.get(value, fallback or value)))
