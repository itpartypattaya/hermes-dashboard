"""Red top banner «a paid model is answering» + the routing-chain state.

The banner appears when the agent is forced onto a paid model:
  • the primary provider is in an exhausted cooldown (auth.json credential_pool
    last_status=exhausted with a future reset) — "we are on fallback right now";
  • or the latest interactive session (providers.fallback_sources, with a model
    call) went to a paid provider.
Only status fields of auth.json are read; no secrets are printed.
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime

from .common import active, connect_ro, esc, home, is_paid_row, paid, paid_label, primary_id, tz
from .config import current
from .i18n import _


def _src_in() -> str:
    srcs = current().get("providers.fallback_sources", []) or []
    cond = "source IN (" + ",".join("'" + s.replace("'", "''") + "'" for s in srcs) + ")" if srcs else "1"
    return cond + " AND " + active()


def primary_cooldown() -> str | None:
    """Reset time (local tz) if the primary is in an exhausted cooldown; '' if unknown; None if healthy."""
    try:
        d = json.loads((home() / "auth.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for e in (d.get("credential_pool", {}) or {}).get(primary_id(), []) or []:
        if not isinstance(e, dict) or e.get("last_status") != "exhausted":
            continue
        try:
            reset = float(e.get("last_error_reset_at"))
        except (TypeError, ValueError):
            reset = None
        if reset and reset > time.time():
            return datetime.fromtimestamp(reset, tz()).strftime("%d.%m %H:%M")
        if e.get("last_error_code") == 429 and not reset:
            return ""
    return None


def _latest_row(cols: str):
    db = connect_ro()
    if db is None:
        return None
    try:
        with db:
            return db.execute(
                f"SELECT {cols} FROM sessions WHERE {_src_in()} "
                "AND started_at >= strftime('%s','now','-6 hours') ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        db.close()


def latest_paid_session():
    row = _latest_row("model, billing_provider, started_at, (" + paid() + ") p")
    if not row or not row["p"]:
        return None
    return row["model"], row["billing_provider"], datetime.fromtimestamp(float(row["started_at"]), tz()).strftime("%d.%m %H:%M")


def build() -> str:
    prim = str(current().get("providers.primary.label", "primary"))
    cooldown = primary_cooldown()
    if cooldown is not None:
        first = current().get("providers.paid", [{}])
        first_label = str((first[0] if first else {}).get("label", "") or _("paid fallback"))
        reset_txt = " " + _("{p} recovery ~<b>{t}</b>.").format(p=prim, t=esc(cooldown)) if cooldown else ""
        return _wrap(_("⚠️ <b>A paid model is answering.</b> {p} is unavailable (quota/limit) — answers go through the paid fallback ({f}).").format(p=esc(prim), f=esc(first_label)) + reset_txt)
    latest = latest_paid_session()
    if latest:
        model, prov, when = latest
        return _wrap(_("⚠️ <b>The last answer came from a paid model</b> ({m}, {w}). {p} was temporarily unavailable; check whether the primary provider is back.").format(
            m=esc(paid_label(prov, model)), w=esc(when), p=esc(prim)))
    return ""


def _wrap(msg: str) -> str:
    return f'<div class="paidbanner"><span class="pb-dot"></span><span class="pb-txt">{msg}</span></div>'


def state() -> dict:
    """Routing chain state for the Overview highlight (must agree with the banner).

    active: 'primary' | <paid provider id> | 'free'; primary: ok | cooldown
    """
    cooldown = primary_cooldown()
    row = _latest_row("billing_provider, model")
    active_key = "primary"
    if row:
        prov, model = row["billing_provider"] or "", row["model"] or ""
        if prov and prov != primary_id():
            active_key = prov if is_paid_row(prov, model) else "free"
    if cooldown is not None and active_key == "primary":
        first = current().get("providers.paid", [])
        active_key = first[0]["id"] if first else "primary"
    return {"active": active_key, "primary": "cooldown" if cooldown is not None else "ok", "reset": cooldown or ""}
