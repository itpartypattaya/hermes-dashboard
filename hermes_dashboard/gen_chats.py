"""«Requests by chat» card — who really uses the agent, exactly, from state.db.

Since Hermes 0.20 `sessions` carries chat_id / chat_type / display_name, so the
breakdown is exact over 30 days. Per-person attribution inside group chats is
not available (a session belongs to a thread, `user_id` is empty there), so it
is deliberately not drawn.
"""
from __future__ import annotations

import sqlite3

from .common import active, connect_ro, fmt_tok, metrics, simple_bar
from .config import current
from .i18n import _

PALETTE = ["var(--brand)", "var(--avail)", "var(--ok)", "var(--part)", "var(--brand-d)", "var(--ink3)"]
TOP_N = 8


def build() -> str:
    db = connect_ro()
    if db is None:
        return ""
    try:
        with db:
            rows = db.execute(
                "SELECT coalesce(chat_id,'') cid, coalesce(chat_type,'') ctype, "
                "coalesce(display_name,'') dn, coalesce(source,'') src, count(*) sess, "
                "coalesce(sum(api_call_count),0) calls, "
                "coalesce(sum(input_tokens+output_tokens+reasoning_tokens),0) tok "
                "FROM sessions WHERE source NOT IN ('cron','cli','subagent','tool','tui') AND " + active() +
                " AND started_at>=strftime('%s','now','-30 days') "
                "GROUP BY cid, ctype, dn, src ORDER BY calls DESC"
            ).fetchall()
    except sqlite3.Error:
        return ""
    finally:
        db.close()
    if not rows:
        return ""
    names = current().get("chats.names", {}) or {}

    def label(r):
        base = names.get(r["cid"]) or r["dn"] or r["cid"] or _("no chat_id")
        return base + (" · " + _("DM") if r["ctype"] == "dm" else "")

    top, rest = rows[:TOP_N], rows[TOP_N:]
    cmax = max((r["calls"] for r in top), default=1) or 1
    bars = "".join(
        simple_bar(label(r), r["calls"], cmax, PALETTE[i % len(PALETTE)],
                   value_txt=f'{r["calls"]} · {r["sess"]} ' + _("sess."))
        for i, r in enumerate(top))
    if rest:
        bars += simple_bar(_("…{n} more chats").format(n=len(rest)), sum(r["calls"] for r in rest), cmax, "var(--ink3)")
    total_calls = sum(r["calls"] for r in rows)
    total_sess = sum(r["sess"] for r in rows)
    return (
        f'<div class="card"><div class="uhead"><h3>{_("Requests by chat · 30 days")}</h3>'
        f'<span class="pill p-ok">{_("state.db · exact")}</span></div>'
        + bars
        + f'<div class="cxpt" style="margin-top:13px">{_("Tokens · in+out+reasoning · top 4")}</div>'
        + metrics([(label(r), fmt_tok(r["tok"])) for r in top[:4]])
        + f'<div class="algo" style="margin-top:12px">'
        + _("Model requests from chats over 30 days, by chat ({c} requests in {s} sessions with a model call). Counted by chat_id in state.db; sessions without a single model call (watching groups) are excluded. People inside groups are not distinguished — a session belongs to a thread.").format(c=total_calls, s=total_sess)
        + '</div></div>'
    )
