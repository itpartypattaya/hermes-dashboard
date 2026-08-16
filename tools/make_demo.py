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
    "views": {"config_map": False, "connectors": True},
}


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
