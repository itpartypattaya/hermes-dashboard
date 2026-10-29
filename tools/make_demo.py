#!/usr/bin/env python3
"""Build a demo dashboard from synthetic data — no agent required.

    python tools/make_demo.py /tmp/demo && open /tmp/demo/index.html

Creates a throwaway HERMES_HOME (state.db, memory files, cron/jobs.json,
config.yaml, a billing export cache) filled with plausible fake telemetry, then
runs the normal build against it. Handy for screenshots, for reviewing a change
without touching a real installation, and for seeing what the dashboard looks
like before installing anything.

Everything here is invented: the numbers, chats and job names exist only in this
script.
"""
from __future__ import annotations

import json
import os
import random
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RNG = random.Random(7)

SESSIONS_SCHEMA = """
CREATE TABLE sessions(
  id TEXT PRIMARY KEY, source TEXT, user_id TEXT, model TEXT, started_at REAL,
  message_count INT DEFAULT 0, tool_call_count INT DEFAULT 0, input_tokens INT DEFAULT 0,
  output_tokens INT DEFAULT 0, cache_read_tokens INT DEFAULT 0, reasoning_tokens INT DEFAULT 0,
  billing_provider TEXT, estimated_cost_usd REAL, cost_status TEXT, cost_source TEXT,
  api_call_count INT DEFAULT 0, chat_id TEXT, chat_type TEXT, display_name TEXT);
CREATE TABLE messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, tool_name TEXT, timestamp REAL);
"""

# Deliberately short, obviously-fake ids: a realistic-looking chat id in a public
# repo trips the leak scanner (and rightly so — it cannot tell invented from real).
CHATS = [("-1002001", "group", "Household"), ("-1002002", "group", "Book club"),
         ("-1002003", "group", "Neighbourhood"), ("770001", "dm", "Owner")]
TOOLS = ["terminal", "read_file", "write_file", "memory", "web_search", "vision_analyze",
         "mcp_google_workspace_sheets_read", "mcp_google_calendar_list", "tg_send_message",
         "skill_view", "execute_code", "delegate_task"]

JOBS = {"jobs": [
    {"id": "a1b2c3d4e5f6", "name": "Agent: nightly memory consolidation", "enabled": True,
     "schedule": {"kind": "cron", "expr": "0 3 * * *"}, "script": "scripts/dream-precheck.py",
     "last_status": "ok", "last_run_at": "2026-08-16T03:00:11+02:00"},
    {"id": "b2c3d4e5f6a1", "name": "Agent: morning reminder", "enabled": True, "no_agent": True,
     "schedule": {"kind": "cron", "expr": "15 7 * * 1-5"}, "last_status": "ok",
     "last_run_at": "2026-08-16T07:15:02+02:00"},
    {"id": "c3d4e5f6a1b2", "name": "Agent: weekly digest of the book club", "enabled": True,
     "schedule": {"kind": "cron", "expr": "0 21 * * 0"}, "script": "scripts/digest-precheck.py",
     "last_status": "ok", "last_run_at": "2026-08-11T21:00:05+02:00"},
    {"id": "d4e5f6a1b2c3", "name": "Watchdog: command scanner", "enabled": True, "no_agent": True,
     "schedule": {"kind": "interval", "minutes": 15}, "last_status": "ok",
     "last_run_at": "2026-08-16T18:15:00+02:00"},
    {"id": "e5f6a1b2c3d4", "name": "Watchdog: token ingest", "enabled": True, "no_agent": True,
     "schedule": {"kind": "cron", "expr": "30 8 * * *"}, "last_status": "ok",
     "last_run_at": "2026-08-16T08:30:00+02:00"},
    {"id": "f6a1b2c3d4e5", "name": "Agent: monthly report to the owner", "enabled": True,
     "schedule": {"kind": "cron", "expr": "0 9 1 * *"}, "last_status": "error",
     "last_error": "provider returned 503 twice", "last_run_at": "2026-08-01T09:00:31+02:00"},
    {"id": "a1b2c3d4e5f7", "name": "Agent: receipt OCR", "enabled": True, "provider": "gemini",
     "model": "gemini-2.5-flash", "schedule": {"kind": "cron", "expr": "0 20 * * *"},
     "last_status": "ok", "last_run_at": "2026-08-16T20:00:07+02:00"},
]}

CONFIG_YAML = """model:
  default: gpt-5.6-terra
  provider: openai-codex
fallback_providers:
  - provider: anthropic
    model: claude-sonnet-5
  - provider: gemini
    model: gemini-3.5-flash
auxiliary:
  vision:
    provider: openai-codex
    model: gpt-5.6-terra
stt:
  enabled: true
  provider: openai
  openai:
    model: whisper-1
tts:
  provider: edge
  edge:
    voice: en-US-AriaNeural
web:
  search_backend: tavily
memory:
  provider: holographic
  memory_char_limit: 4200
  user_char_limit: 2800
platforms:
  telegram:
    enabled: true
  line:
    enabled: false
mcp_servers:
  google-workspace:
    command: npx
    args: ["-y", "workspace-mcp", "sheets:full", "drive:full", "gmail:send"]
    enabled: true
  google-calendar:
    command: npx
    enabled: true
  notion:
    command: npx
    enabled: false
plugins:
  enabled: ["voice-replies", "receipt-ingest"]
  disabled: ["line-bridge"]
tirith_enabled: true
tirith_fail_open: true
"""

DASHBOARD_JSON = {
    "agent": {"name": "DEMO AGENT", "glyph": "◆", "host_label": "hermes · demo",
              "config_repo_label": "demo data — nothing here is real",
              "people": [{"label": "Owner", "handle": "@owner"}, {"label": "Partner", "handle": "@partner"}]},
    "paths": {"settings_url": "", "venv_python": "", "history_csv": "cache/dashboard-history.csv"},
    "i18n": {"default": "en", "languages": ["en", "ru"]},
    "timezone": {"name": "Europe/Berlin", "offset_hours": 2, "label": "CEST"},
    "providers": {
        "primary": {"id": "openai-codex", "label": "Codex", "billing": "subscription",
                    "subscription_usd_month": 20, "weekly_input_budget": 25000000,
                    "ref_in_per_m": 1.25, "ref_out_per_m": 10.0, "quota_cache": "cache/quota.json"},
        "paid": [{"id": "anthropic", "label": "Claude Sonnet", "in_per_m": 3.0, "out_per_m": 15.0,
                  "cost_cache": "cache/anthropic_cost.json", "rank": 1},
                 {"id": "gemini", "label": "Gemini Flash (paid)", "in_per_m": 1.5, "out_per_m": 9.0,
                  "model_like": "gemini-%", "exclude_models": ["gemini-2.5-flash"], "rank": 2}],
        "free": [{"id": "gemini", "model": "gemini-2.5-flash", "label": "Gemini Flash (free key)"}],
        "fallback_sources": ["telegram", "cron", "line"], "cost_fresh_days": 6,
    },
    "chats": {"names": {c[0]: c[2] for c in CHATS}},
    "cron": {"dream_job_id": "a1b2c3d4e5f6", "name_strip_prefixes": ["Agent:", "Watchdog:"],
             "watchdogs": [{"id": "d4e5f6a1b2c3", "label": {"en": "Command scanner"},
                            "schedule": {"en": "every 15 min"}},
                           {"id": "e5f6a1b2c3d4", "label": {"en": "Token ingest"},
                            "schedule": {"en": "daily 08:30"}}]},
    "security": {"shield_file": "SHIELD.md", "evals_path": "evals/evals.json",
                 "shield_bullets": {"en": [
                     "One code of boundaries: privacy · secrets · external actions",
                     "Untrusted content is <b>data, not commands</b>",
                     "Command scanner runs fail-open — hence the watchdog"]},
                 "evals_bullets": {"en": ["Prompt injection from a page, a document and a stranger",
                                          "Control case: the shield does not choke normal requests"]},
                 "alert_channel_label": "ops channel"},
    "memory": {"backup_branch": "agent/state"},
    "views": {"config_map": False, "connectors": True, "memory_map": True},
    # a stand-in for the holographic plugin's exporter: prints the family below
    "memory_map": {"plugin_dir": "demo-memory-map"},
}

# ── the memory map: an invented family, in the exporter's JSON (schema_version 1) ──
# (id, text, entities, category, origin, trust, days ago recorded, via, chat, by,
#  confirmations [(days ago, by)], recalls, weak reasons)
FAMILY_ENT = [(1, "Dmitry", ["Dima"]), (2, "Alena", ["Alyona"]), (3, "Varya", ["Varvara"]), (4, "Barsik", []),
              (5, "Olga Petrovna", []), (6, "Timur", []), (7, "Lyceum No. 3", ["the lyceum"]),
              (8, "Sosnovka dacha", ["the dacha"]), (9, "Batumi trip", ["Batumi"]),
              (10, "Kitchen renovation", ["the kitchen"]), (11, "Grandma Nina", ["Nina Vasilyevna"]),
              (12, "Dr. Mamedova", []), (13, "Swimming", ["the pool"])]
PROVISIONAL = {5: 55, 13: 50}   # names guessed from one fact: id -> days ago
FAMILY = [
    (1, "Varya is in class 2B at Lyceum No. 3.", [3, 7], "school", "said", .65, 58, "tool", "dm", "Dmitry", [(40, "Alena")], 14, []),
    (2, "Varya's class teacher is Olga Petrovna.", [3, 5, 7], "school", "said", .5, 55, "auto_extract", "dm", "Alena", [], 3, []),
    (3, "School ends at 13:30; Alena picks Varya up.", [3, 2, 7], "school", "said", .55, 55, "tool", "dm", "Alena", [], 6, []),
    (4, "Varya swims on Tuesdays and Thursdays at 17:00.", [3, 13], "activities", "said", .5, 50, "tool", "dm", "Dmitry", [], 9, []),
    (5, "Varya's swimming moved to Wednesday, 17:30.", [3, 13], "activities", "said", .5, 10, "auto_extract", "group", "Alena", [], 1, []),
    (6, "Varya is allergic to peanuts.", [3, 12], "health", "said", .7, 136, "tool", "dm", "Dmitry", [(118, "Dmitry"), (46, "Alena")], 22, []),
    (7, "Varya's paediatrician is Dr. Mamedova; appointments on Saturdays.", [3, 12], "health", "said", .6, 136, "tool", "dm", "Dmitry", [], 4, []),
    (8, "Alena does not eat dairy.", [2], "food", "inferred", .4, 88, "tool", "dm", "Dmitry", [], 5, ["unconfirmed_inference"]),
    (9, "Alena asked to buy cottage cheese and cheese for a casserole.", [2], "food", "said", .5, 6, "auto_extract", "group", "Alena", [], 0, []),
    (10, "Alena works at an architecture studio, remote on Mondays and Fridays.", [2], "work", "said", .55, 45, "tool", "dm", "Alena", [], 7, []),
    (11, "Dmitry prefers reminders after 9 am.", [1], "preferences", "said", .65, 210, "tool", "dm", "Dmitry", [(150, "Dmitry"), (4, "Dmitry")], 31, []),
    (12, "Dmitry seems to dislike phone calls in the morning.", [1], "preferences", "inferred", .4, 72, "tool", "dm", "Dmitry", [], 2, ["unconfirmed_inference"]),
    (13, "Barsik eats only the Fish brand dry food.", [4], "home", "said", .55, 232, "tool", "dm", "Alena", [(81, "Varya")], 8, []),
    (14, "Barsik needs a yearly vaccination; the last one was on 14 March.", [4], "health", "said", .5, 229, "tool", "dm", "Alena", [], 1, []),
    (15, "Timur works in IT.", [6], "people", "unknown", .25, 115, "auto_extract", "group", None, [], 0, ["low_trust", "no_source"]),
    (16, "Timur looks after Barsik when the family is away.", [6, 4], "home", "said", .55, 115, "tool", "dm", "Dmitry", [], 3, []),
    (17, "An old bicycle is kept in the shed at the dacha.", [8], "dacha", "said", .5, 132, "tool", "dm", "Dmitry", [], 0, ["never_recalled"]),
    (18, "There is no internet at the dacha; there is signal only by the gate.", [8], "dacha", "said", .5, 132, "tool", "dm", "Dmitry", [], 2, []),
    (19, "The dacha is closed for the winter at the end of October.", [8], "dacha", "said", .55, 370, "tool", "dm", "Dmitry", [(11, "Dmitry")], 6, []),
    (20, "Batumi trip: 8-15 November, the hotel is booked and paid.", [9], "trip", "said", .6, 18, "tool", "dm", "Dmitry", [], 5, []),
    (21, "We fly to Batumi via Istanbul with a 3-hour layover.", [9], "trip", "said", .5, 8, "tool", "dm", "Dmitry", [], 1, []),
    (22, "Grandma Nina is coming to Batumi with the family.", [9, 11], "trip", "said", .5, 7, "auto_extract", "group", "Alena", [], 1, []),
    (24, "Arsen's crew does the kitchen; payment in stages.", [10], "renovation", "said", .55, 28, "tool", "dm", "Alena", [], 4, []),
    (25, "The kitchen renovation budget is 420 thousand.", [10], "renovation", "said", .5, 25, "tool", "dm", "Dmitry", [], 3, []),
    (26, "Grandma Nina lives in Tver and comes for the summer.", [11], "family", "unknown", .5, 400, "legacy", None, None, [], 4, []),
    (27, "Grandma Nina takes her blood-pressure pills in the morning.", [11], "health", "unknown", .4, 400, "legacy", None, None, [], 2, []),
    (28, "Varya is in second grade, 2B, lyceum number 3.", [3, 7], "school", "said", .5, 4, "auto_extract", "group", "Dmitry", [], 0, ["near_duplicate"]),
    (31, "The family lives at 12 Garden Street.", [1, 2, 3], "home", "said", .6, 243, "tool", "dm", "Dmitry", [(178, "Alena")], 17, []),
    (32, "Dmitry cycles to work.", [1], "preferences", "confirmed", .65, 142, "tool", "dm", "Dmitry", [(140, "Dmitry")], 4, []),
    (33, "Friday is Varya's library day: the books must go back.", [3, 7], "school", "said", .5, 3, "auto_extract", "dm", "Alena", [], 1, []),
    (34, "Varya wants a scooter for her birthday.", [3], "family", "said", .5, 5, "auto_extract", "group", "Varya", [], 0, []),
    (36, "The pump at the dacha is broken; Timur promised to have a look.", [8, 6], "dacha", "said", .5, 4, "auto_extract", "group", "Dmitry", [], 0, []),
    (39, "No internet at the dacha, only by the gate.", [8], "dacha", "said", .5, 9, "auto_extract", "group", "Dmitry", [], 0, ["near_duplicate"]),
    (40, "The Wi-Fi router is in the hallway cupboard.", [], "home", "unknown", .5, 400, "legacy", None, None, [], 3, []),
]
DUP_OF = {28: 1, 39: 18}
# facts with more than one version: id -> [(status, old text or None for the current one, from, to)] in days ago
VERSIONS = {
    1: [("changed", "Varya started class 1B at Lyceum No. 3.", 425, 60), ("current", None, 59, None)],
    10: [("changed", "Alena works at an architecture studio.", 172, 46), ("current", None, 45, None)],
    20: [("changed", "The Batumi trip is planned for early November.", 32, 19), ("current", None, 18, None)],
    25: [("corrected", None, 28, 25), ("current", None, 25, None)],
}
ENDED = [(23, "The kitchen tiles arrive on 24 October.", [10], "renovation", 15, 5),
         (30, "Varya goes to the Sunshine kindergarten.", [3], "family", 420, 60)]


def demo_memory_map(offset_h: int) -> dict:
    """The exporter's snapshot of the invented family, dated relative to today."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone(timedelta(hours=offset_h))).replace(microsecond=0)

    def d(days):
        return None if days is None else (now.date() - timedelta(days=days)).isoformat()

    def level(t):
        return "strong" if t >= .65 else "normal" if t >= .45 else "weak"

    facts = []
    for fid, text, ents, cat, origin, trust, rec, via, chat, by, conf, recalls, weak in FAMILY:
        vs = VERSIONS.get(fid) or [("current", None, rec, None)]
        f = {"id": fid, "text": text, "ents": ents, "cat": cat, "origin": origin, "trust": trust,
             "trust_level": level(trust), "status": "current", "from": d(vs[-1][2]) if fid in VERSIONS else None,
             "to": None, "to_exact": None, "rec": d(rec), "first": d(vs[0][2]), "via": via, "chat": chat, "by": by,
             "conf": [{"d": d(a), "by": who} for a, who in conf], "recalls": recalls, "helpful": 0, "last": None,
             "versions": [{"seq": i, "status": st, "text": None if st == "corrected" else (old or text),
                           "from": d(a), "to": d(b), "to_exact": b is not None,
                           "rec": d(rec) if st == "current" else d(a), "closed": d(b),
                           "origin": None if via == "legacy" else origin, "via": via}
                          for i, (st, old, a, b) in enumerate(vs, 1)],
             "mergedFrom": [], "weak": weak}
        if fid in DUP_OF:
            f["dup_of"] = DUP_OF[fid]
        facts.append(f)
    for fid, text, ents, cat, rec, to in ENDED:
        facts.append({"id": fid, "text": text, "ents": ents, "cat": cat, "origin": "said", "trust": .5,
                      "trust_level": "normal", "status": "ended", "from": d(rec), "to": d(to), "to_exact": True,
                      "rec": d(rec), "first": d(rec), "via": "tool", "chat": "dm", "by": "Alena", "conf": [],
                      "recalls": None, "helpful": None, "last": None,
                      "versions": [{"seq": 1, "status": "ended", "text": text, "from": d(rec), "to": d(to),
                                    "to_exact": True, "rec": d(rec), "closed": d(to), "origin": "said",
                                    "via": "tool"}],
                      "mergedFrom": [], "weak": []})
    facts.sort(key=lambda f: f["id"])
    merges = [{"from": 29, "into": 1, "d": d(7), "prop": "D-15", "text": "Varya is in 1B at Lyceum No. 3."}]
    facts[0]["mergedFrom"] = [{"id": 29, "text": merges[0]["text"], "d": d(7), "prop": "D-15"}]
    count: dict = {}
    for f in facts:
        for e in f["ents"]:
            count[e] = count.get(e, 0) + 1
    ents = [{"id": eid, "name": name, "type": None, "declared": eid not in PROVISIONAL, "aliases": aliases,
             "text_aliases": [], "standing": "provisional" if eid in PROVISIONAL else "established",
             "stale": False, "since": d(PROVISIONAL.get(eid, 300)), "facts": count.get(eid, 0)}
            for eid, name, aliases in FAMILY_ENT]
    conflicts = [{"id": "C1", "a": 4, "b": 5, "score": 0.82, "shared": [3, 13]},
                 {"id": "C2", "a": 8, "b": 9, "score": 0.64, "shared": [2]}]
    pending = [{"kind": "conflict", "id": c["id"], "a": c["a"], "b": c["b"], "status": "open"} for c in conflicts] + [
        {"kind": "merge", "id": "D-17", "keep": 1, "copy": 28, "covered": True, "extra": [], "created": d(3),
         "asked": d(2), "status": "open", "current": True},
        {"kind": "merge", "id": "D-18", "keep": 18, "copy": 39, "covered": False, "extra": ["only"],
         "created": d(3), "asked": None, "status": "open", "current": True},
        {"kind": "memory_conflict", "id": "M-1", "fact": 11, "entry": "Dmitry wants reminders from 8 am.",
         "fact_numbers": ["9"], "memory_numbers": ["8"], "asked": d(1), "status": "open"},
        {"kind": "relevant", "id": "R-7", "text": "Remind Dmitry to water the flowers on Fridays.",
         "last_mention_days": 61, "asked": d(4), "status": "open"},
        {"kind": "queue", "id": "Q", "records": 2, "writes": 2, "unreadable": 0, "oldest": d(9), "status": "open"},
    ]

    def night(days, alerts, stats):
        at = (now - timedelta(days=days)).replace(hour=3, minute=5, second=0).isoformat()
        return {"d": d(days), "k": "night", "at": at, "alerts": alerts, "stats": stats}

    feed = [night(1, [], {"messages_read": 212, "days": 14, "candidates": 2}),
            night(2, ["messages_unavailable"], {"messages_read": 0}),
            {"d": d(3), "k": "new", "f": 33}, {"d": d(4), "k": "conf", "f": 11, "by": "Dmitry"},
            {"d": d(4), "k": "new", "f": 36}, {"d": d(4), "k": "new", "f": 28},
            {"d": d(5), "k": "del", "id": 41}, {"d": d(5), "k": "end", "f": 23}, {"d": d(5), "k": "new", "f": 34},
            {"d": d(6), "k": "new", "f": 9}, {"d": d(7), "k": "mrg", "from": 29, "into": 1},
            {"d": d(7), "k": "new", "f": 22}]
    tombs = [{"id": 41, "d": d(5)}, {"id": 37, "d": d(57)}]
    hidden = [{"id": 38, "d": d(12), "why": "credential", "status": "current"}]
    current = [f for f in facts if f["status"] == "current"]
    snap = {"made": now.isoformat(), "tz": "%+03d:00" % offset_h, "store": "memory_store.db",
            "schema": {"store": "2", "history": "1", "entity_rules": "4", "experience": None},
            "counts": {"current": len(current), "ended": len(facts) - len(current), "merged": len(merges),
                       "forgotten": len(tombs), "corrected": 1, "hidden": len(hidden), "entities": len(ents),
                       "provisional": len(PROVISIONAL),
                       "legacy": sum(1 for f in current if f["via"] == "legacy" and f["origin"] == "unknown")},
            "week_from": d(7), "excluded": [{"what": "hidden_by_screen", "count": len(hidden)}],
            "experience": {"included": False},
            "dreaming": {"read": True, "reason": None, "files": {"proposals": "ok", "report": "ok", "queue": "ok"}}}
    return {"schema_version": 1, "SNAP": snap, "ENT": ents, "FACTS": facts, "CONFLICTS": conflicts,
            "PENDING": pending, "TOMBS": tombs, "MERGES": merges, "HIDDEN": hidden, "FEED": feed, "ATTEMPTS": []}


def build_home(home: Path) -> None:
    (home / "cache").mkdir(parents=True, exist_ok=True)
    (home / "memories").mkdir(exist_ok=True)
    (home / "cron").mkdir(exist_ok=True)
    (home / "dashboard").mkdir(exist_ok=True)
    (home / "skills" / "receipt-ocr").mkdir(parents=True, exist_ok=True)
    (home / "plugins" / "voice-replies").mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(CONFIG_YAML, encoding="utf-8")
    (home / "cron" / "jobs.json").write_text(json.dumps(JOBS, indent=1), encoding="utf-8")
    (home / "dashboard" / "dashboard.json").write_text(
        json.dumps(DASHBOARD_JSON, ensure_ascii=False, indent=2), encoding="utf-8")
    (home / "memories" / "MEMORY.md").write_text("§ " + ("demo rule. " * 30) * 12, encoding="utf-8")
    (home / "memories" / "USER.md").write_text("demo profile. " * 150, encoding="utf-8")
    (home / "SHIELD.md").write_text("# Demo shield\n", encoding="utf-8")
    (home / "evals").mkdir(exist_ok=True)
    (home / "evals" / "evals.json").write_text(json.dumps({"evals": [{"id": i} for i in range(11)]}),
                                               encoding="utf-8")
    (home / "skills" / "receipt-ocr" / "SKILL.md").write_text(
        "---\nname: receipt-ocr\ndescription: Turn a photo of a receipt into a spreadsheet row.\n---\n",
        encoding="utf-8")
    (home / "plugins" / "voice-replies" / "plugin.yaml").write_text(
        "name: voice-replies\nkind: standalone\ndescription: Speaks short answers out loud.\n",
        encoding="utf-8")
    (home / ".gitignore").write_text("*\n!/skills/receipt-ocr/\n", encoding="utf-8")
    (home / ".env").write_text("TAVILY_API_KEY=demo\nBROWSERBASE_API_KEY=demo\n", encoding="utf-8")

    # live quota + a fresh billing export cache
    now = time.time()
    (home / "cache" / "quota.json").write_text(json.dumps({
        "plan": "Plus", "windows": [{"key": "session", "label": "session window",
                                     "remaining_percent": 63.0, "reset_local": "20.08 14:15",
                                     "reset_in_h": 91.2}]}), encoding="utf-8")
    from datetime import date, timedelta
    buckets = [{"date": (date.today() - timedelta(days=i)).isoformat(),
                "usd": round(RNG.uniform(0, 0.9), 2) if i % 3 else 0.0} for i in range(0, 30)]
    (home / "cache" / "anthropic_cost.json").write_text(json.dumps({
        "source": "csv", "usd_30d": round(sum(b["usd"] for b in buckets), 2),
        "usd_7d": round(sum(b["usd"] for b in buckets[:7]), 2),
        "data_through": (date.today() - timedelta(days=1)).isoformat(), "data_age_days": 1,
        "by_type": {"input_no_cache": 1.9, "input_cache_write_5m": 2.4, "output": 1.1,
                    "input_cache_read": 0.3},
        "buckets": buckets}), encoding="utf-8")

    db = sqlite3.connect(home / "state.db")
    db.executescript(SESSIONS_SCHEMA)
    rows, msgs = [], []
    for day in range(30, -1, -1):
        for _n in range(RNG.randint(2, 11)):
            ts = now - day * 86400 - RNG.randint(0, 80000)
            src = RNG.choices(["telegram", "cron", "cli", "subagent"], [6, 3, 1, 1])[0]
            chat = RNG.choice(CHATS) if src == "telegram" else (None, None, None)
            paid_roll = RNG.random()
            if paid_roll > 0.965 and src in ("telegram", "cron"):
                prov, model, cost, status = "anthropic", "claude-sonnet-5", round(RNG.uniform(.02, .3), 3), "estimated"
            elif paid_roll > 0.95:
                prov, model, cost, status = "gemini", "gemini-2.5-flash", 0.0, "included"
            else:
                prov, model, cost, status = "openai-codex", "gpt-5.6-terra", 0.0, "included"
            calls = 0 if (src == "telegram" and RNG.random() < 0.22) else RNG.randint(1, 22)
            inp = 0 if not calls else RNG.randint(4000, 260000)
            sid = ("cron_%s_%d" % (RNG.choice(list(JOBS["jobs"]))["id"], ts)) if src == "cron" else "s%d_%d" % (day, ts)
            rows.append((sid, src, "", model, ts, RNG.randint(0, 30), RNG.randint(0, 20), inp,
                         inp // 12, inp * RNG.randint(4, 9), inp // 20, prov, cost, status, None,
                         calls, chat[0], chat[1], chat[2]))
            for _t in range(RNG.randint(0, 6)):
                msgs.append((sid, "tool", RNG.choice(TOOLS), ts + RNG.randint(1, 500)))
    db.executemany("INSERT OR IGNORE INTO sessions VALUES (" + ",".join("?" * 19) + ")", rows)
    db.executemany("INSERT INTO messages(session_id, role, tool_name, timestamp) VALUES (?,?,?,?)", msgs)
    db.commit()
    db.close()

    # the memory map tab runs the plugin's exporter; this stand-in prints the invented family
    exporter = home / "demo-memory-map"
    exporter.mkdir(exist_ok=True)
    snapshot = json.dumps(demo_memory_map(DASHBOARD_JSON["timezone"]["offset_hours"]))
    (exporter / "memory_map.py").write_text(
        "# demo stand-in for the holographic plugin's memory_map.py: prints a fixed snapshot\n"
        "import sys\nsys.stdout.write(%r)\n" % snapshot, encoding="utf-8")

    mem = sqlite3.connect(home / "memory_store.db")
    mem.execute("CREATE TABLE facts(id INTEGER PRIMARY KEY, text TEXT)")
    mem.executemany("INSERT INTO facts(text) VALUES (?)", [("demo fact %d" % i,) for i in range(64)])
    mem.commit()
    mem.close()


def main(argv=None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    out = Path(args[0]).expanduser() if args else Path("./demo")
    home = out / "_home"
    if home.exists():
        import shutil
        shutil.rmtree(home)
    build_home(home)
    env = dict(os.environ, HERMES_HOME=str(home), PYTHONPATH=str(ROOT))
    r = subprocess.run([sys.executable, "-m", "hermes_dashboard.build",
                        "--config", str(home / "dashboard" / "dashboard.json"), "--out", str(out)],
                       env=env, text=True)
    if r.returncode == 0:
        print("demo pages in %s (index.html, index.ru.html, connectors.html)" % out)
    return r.returncode


if __name__ == "__main__":
    raise SystemExit(main())
