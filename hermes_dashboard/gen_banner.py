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

from .common import (active, connect_ro, esc, home, is_paid_row, paid, paid_label, primary_id,
                     read_text_safe, src_in, tz)
from .config import current
from .i18n import _


def _src_in() -> str:
    return src_in() + " AND " + active()


# Statuses the core writes on a pool entry that cannot serve. "dead" and an
# explicit 401 mean the credential was *rejected*: unlike an exhausted quota it
# never comes back on its own, and every answer meanwhile is billed elsewhere.
_REJECTED_STATUSES = {"dead", "invalid", "revoked", "unauthorized"}
_REJECTED_CODES = {401, 403}


def primary_credential_problem() -> tuple[str, str] | None:
    """(kind, detail) when the primary provider's credential cannot answer.

    kind is "rejected" — only a new login fixes it — or "cooldown", which
    resets by itself. None means healthy.

    Both were previously funnelled through a single cooldown check that spoke
    only when it could name a reset time or saw a 429, so the most expensive
    state of all — a credential invalidated at 401, every answer silently on the
    paid fallback until a human notices — displayed nothing at all.
    """
    try:
        d = json.loads(read_text_safe(home() / "auth.json", "null"))
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    for e in (d.get("credential_pool", {}) or {}).get(primary_id(), []) or []:
        if not isinstance(e, dict):
            continue
        status = str(e.get("last_status") or "").lower()
        code = e.get("last_error_code")
        if status in _REJECTED_STATUSES or code in _REJECTED_CODES:
            reason = str(e.get("last_error_message") or e.get("last_error_reason") or "").strip()
            return "rejected", reason[:160]
        if status == "exhausted":
            try:
                reset = float(e.get("last_error_reset_at"))
            except (TypeError, ValueError):
                reset = None
            if reset and reset > time.time():
                return "cooldown", datetime.fromtimestamp(reset, tz()).strftime("%d.%m %H:%M")
            return "cooldown", ""       # no reset time is still a cooldown, not silence
    return None


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
    first = current().get("providers.paid", [{}])
    first_label = str((first[0] if first else {}).get("label", "") or _("paid fallback"))
    problem = primary_credential_problem()
    if problem is not None:
        kind, detail = problem
        if kind == "rejected":
            why = " " + _("Provider says: <b>{r}</b>.").format(r=esc(detail)) if detail else ""
            return _wrap(_("⛔ <b>The {p} credential was rejected.</b> It will not recover on its "
                           "own — sign in again; until then every answer is billed to the paid "
                           "fallback ({f}).").format(p=esc(prim), f=esc(first_label)) + why)
        reset_txt = " " + _("{p} recovery ~<b>{t}</b>.").format(p=prim, t=esc(detail)) if detail else ""
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
