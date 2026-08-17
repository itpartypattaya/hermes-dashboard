"""Engine tests on a synthetic HERMES_HOME (no real agent needed).

Covers: config defaults/validation, SQL classification, i18n coverage, a full
build on an empty home and on a synthetic state.db, div balance, no Cyrillic in
the English page outside agent data, settings CSRF gate.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hermes_dashboard import common, config, i18n  # noqa: E402


def make_home(with_db: bool = True) -> Path:
    home = Path(tempfile.mkdtemp(prefix="hd-test-"))
    (home / "cache").mkdir()
    (home / "memories").mkdir()
    (home / "memories" / "MEMORY.md").write_text("§ rule one\n§ rule two\n" * 5, encoding="utf-8")
    (home / "memories" / "USER.md").write_text("name: test\n", encoding="utf-8")
    (home / "config.yaml").write_text(
        "model:\n  default: gpt-test\n  provider: openai-codex\nstt:\n  provider: openai\n  openai:\n    model: whisper-1\n"
        "auxiliary:\n  vision:\n    provider: openai-codex\n    model: gpt-test\nmemory:\n  memory_char_limit: 100\n  user_char_limit: 50\n",
        encoding="utf-8")
    (home / "cron").mkdir()
    (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": [
        {"id": "aaa", "name": "nightly dream", "enabled": True, "schedule": {"kind": "cron", "expr": "0 3 * * *"},
         "last_status": "ok", "last_run_at": "2026-08-15T03:00:00+07:00"},
        {"id": "bbb", "name": "reminder", "enabled": True, "no_agent": True, "schedule": {"kind": "cron", "expr": "*/15 * * * *"}},
        {"id": "ccc", "name": "broken", "enabled": True, "schedule": {"kind": "cron", "expr": "0 9 * * 1"},
         "last_status": "error", "last_error": "boom", "last_run_at": "2026-08-15T09:00:00+07:00"},
    ]}), encoding="utf-8")
    if with_db:
        db = sqlite3.connect(home / "state.db")
        db.executescript("""
        CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT, user_id TEXT, model TEXT, started_at REAL,
          message_count INT DEFAULT 0, tool_call_count INT DEFAULT 0, input_tokens INT DEFAULT 0,
          output_tokens INT DEFAULT 0, cache_read_tokens INT DEFAULT 0, reasoning_tokens INT DEFAULT 0,
          billing_provider TEXT, estimated_cost_usd REAL, cost_status TEXT, api_call_count INT DEFAULT 0,
          chat_id TEXT, chat_type TEXT, display_name TEXT, cost_source TEXT);
        CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, tool_name TEXT, timestamp REAL);
        """)
        now = time.time()
        rows = [
            ("s1", "telegram", "1", "gpt-test", now - 3600, 5, 2, 1000, 200, 5000, 50, "openai-codex", 0.0, "included", 3, "-100", "group", "Family"),
            ("s2", "telegram", "", "gpt-test", now - 7200, 3, 0, 0, 0, 0, 0, None, None, None, 0, "-100", "group", "Family"),  # passive
            ("s3", "cron", "", "claude-sonnet-5", now - 86400, 2, 0, 500, 100, 0, 0, "anthropic", 0.5, "estimated", 1, None, None, None),  # forced fallback
            ("s4", "cli", "", "claude-sonnet-5", now - 86400 * 2, 2, 0, 900, 100, 0, 0, "anthropic", 0.9, "estimated", 1, None, None, None),  # deliberate
            ("s5", "telegram", "", "gemini-2.5-flash", now - 86400 * 3, 2, 0, 300, 30, 0, 0, "gemini", None, None, 1, "-200", "group", "Other"),  # free
            ("s6", "cron", "", "gpt-5.5", now - 86400 * 4, 2, 0, 300, 30, 0, 0, "gemini", None, None, 1, None, None, None),  # stale label
        ]
        db.executemany("INSERT INTO sessions VALUES (" + ",".join("?" * 18) + ", NULL)", rows)
        db.executemany("INSERT INTO messages(session_id, role, tool_name, timestamp) VALUES (?,?,?,?)", [
            ("s1", "tool", "memory", now - 100), ("s1", "tool", "mcp_google_workspace_sheets_read", now - 90),
            ("s1", "tool", "vision_analyze", now - 80), ("s3", "tool", "terminal", now - 70)])
        db.commit()
        db.close()
    return home


class ConfigTests(unittest.TestCase):
    def test_defaults_and_validate(self):
        c = config.Config({})
        self.assertEqual(c.default_lang, "en")
        self.assertEqual(c.validate(), [])
        # an empty primary id is a legitimate half-configured state, not a save-blocker
        self.assertEqual(config.Config({"providers": {"primary": {"id": ""}}}).validate(), [])
        bad = config.Config({"providers": {"paid": [{"id": "", "in_per_m": "abc"}]}})
        self.assertTrue(any("paid[0].id" in e for e in bad.validate()))
        self.assertTrue(any("in_per_m" in e for e in bad.validate()))
        self.assertTrue(any("offset_hours" in e for e in
                            config.Config({"timezone": {"offset_hours": "x"}}).validate()))

    def test_languages_default_first(self):
        c = config.Config({"i18n": {"default": "ru", "languages": ["en", "ru"]}})
        self.assertEqual(c.languages, ["ru", "en"])

    def test_text_localised(self):
        c = config.Config({})
        self.assertEqual(c.text({"en": "a", "ru": "б"}, "ru"), "б")
        self.assertEqual(c.text({"en": "a"}, "ru"), "a")
        self.assertEqual(c.text("plain", "ru"), "plain")


PROVIDERS = {"providers": {
    "primary": {"id": "openai-codex", "label": "Primary"},
    "paid": [
        {"id": "anthropic", "label": "Claude", "in_per_m": 3.0, "out_per_m": 15.0, "rank": 1},
        {"id": "gemini", "label": "Gemini paid", "in_per_m": 1.5, "out_per_m": 9.0,
         "model_like": "gemini-%", "exclude_models": ["gemini-2.5-flash"], "rank": 2},
    ],
    "free": [{"id": "gemini", "model": "gemini-2.5-flash", "label": "Gemini free"}],
    "fallback_sources": ["telegram", "cron", "line"],
}}


class SqlTests(unittest.TestCase):
    def setUp(self):
        self.home = make_home()
        os.environ["HERMES_HOME"] = str(self.home)
        config.set_current(config.Config(PROVIDERS))

    def test_empty_config_has_no_providers(self):
        """A provider-neutral default must never guess that traffic is paid."""
        config.set_current(config.Config({}))
        self.assertEqual(common.paid(), "(0)")
        self.assertEqual(common.scalar("SELECT count(*) FROM sessions WHERE " + common.paid()), 0)
        config.set_current(config.Config(PROVIDERS))

    def test_classification(self):
        n = common.scalar(f"SELECT count(*) FROM sessions WHERE {common.active()}")
        self.assertEqual(n, 5)
        paid = common.scalar(f"SELECT count(*) FROM sessions WHERE {common.paid()}")
        self.assertEqual(paid, 2, "anthropic ×2; gemini stale label and free key are not paid")
        forced = common.scalar(f"SELECT count(*) FROM sessions WHERE {common.fallback()}")
        self.assertEqual(forced, 1, "only the cron session is a forced fallback; CLI is deliberate")
        free = common.scalar(f"SELECT count(*) FROM sessions WHERE {common.free_cond()}")
        self.assertEqual(free, 1)

    def test_is_paid_row(self):
        self.assertTrue(common.is_paid_row("anthropic", "claude-sonnet-5"))
        self.assertTrue(common.is_paid_row("gemini", "gemini-3.5-flash"))
        self.assertFalse(common.is_paid_row("gemini", "gemini-2.5-flash"))
        self.assertFalse(common.is_paid_row("gemini", "gpt-5.5"))

    def test_yaml_get(self):
        self.assertEqual(common.yaml_get("stt.openai.model"), "whisper-1")
        self.assertEqual(common.yaml_get("model.default"), "gpt-test")
        self.assertEqual(common.yaml_get("nope.x", "d"), "d")


class I18nTests(unittest.TestCase):
    def test_ru_locale_complete(self):
        r = subprocess.run([sys.executable, str(ROOT / "tools" / "extract_strings.py"), "ru"],
                           capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(r.returncode, 0, "missing ru translations:\n" + r.stdout)

    def test_fallback_to_english(self):
        i18n.set_lang("xx")
        self.assertEqual(i18n._("Overview"), "Overview")
        i18n.set_lang("ru")
        self.assertEqual(i18n._("Overview"), "Обзор")
        i18n.set_lang("en")


class HardeningTests(unittest.TestCase):
    """Regressions for the review round: each test fails on the old code."""

    def test_language_code_is_whitelisted_and_escaped(self):
        """?lang= is attacker-controlled and lands in <html lang> and a cookie."""
        from hermes_dashboard import render
        cfg = config.Config({"i18n": {"default": "en", "languages": ["en", "ru"]}})
        config.set_current(cfg)
        evil = '" onmouseover="alert(1)'
        head = render.head(cfg, "t", evil)
        self.assertNotIn('lang="" onmouseover=', head)
        self.assertIn("&quot;", head.split("<head>")[0])
        # and the JS literal cannot be broken out of
        self.assertNotIn("</script>", render.jss("</script><script>"))
        self.assertEqual(render.jss("a\r\nb"), "ab")

    def test_settings_lang_falls_back_to_a_known_language(self):
        from hermes_dashboard import settings as st

        class FakeHeaders(dict):
            def get(self, k, d=None):
                return dict.get(self, k, d)

        h = st.Handler.__new__(st.Handler)
        h.state = type("S", (), {"cfg": config.Config(
            {"i18n": {"default": "en", "languages": ["en", "ru"]}})})()
        h.headers = FakeHeaders()
        h.path = '/?lang=%22%20onmouseover%3D%22alert(1)'
        self.assertEqual(h._lang(), "en")
        h.path = "/?lang=ru"
        self.assertEqual(h._lang(), "ru")
        h.path = "/"
        h.headers = FakeHeaders({"Cookie": "hd-lang=zz"})
        self.assertEqual(h._lang(), "en", "an unknown cookie language must not pass through")

    def test_cost_api_asks_for_a_legal_page_size(self):
        """Daily buckets cap at 31; limit=32 is a 400 that used to be swallowed."""
        from hermes_dashboard.collectors import anthropic_cost as ac
        self.assertLessEqual(ac.MAX_DAILY_BUCKETS, 31)
        src = (ROOT / "hermes_dashboard" / "collectors" / "anthropic_cost.py").read_text(encoding="utf-8")
        self.assertIn("has_more", src, "pagination must be followed")

    def test_concurrent_writers_never_collide_or_tear(self):
        """Two builds writing the same page must not fight over a temp file.

        The old code derived the temp name from the target (`index.html.tmp`),
        so a cron build and a manual rebuild used the same path: one of them
        failed to rename, or published bytes the other was still writing.
        """
        import threading
        from hermes_dashboard import build as bld
        d = Path(tempfile.mkdtemp(prefix="hd-atomic-"))
        target = d / "index.html"
        payloads = ["A" * 60000, "B" * 60000]
        errors: list = []

        def writer(text):
            for _ in range(15):
                try:
                    bld._atomic_write(target, text)
                except Exception as e:            # noqa: BLE001
                    errors.append(repr(e))

        ts = [threading.Thread(target=writer, args=(t,)) for t in payloads]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errors, [], "concurrent writes must not fail")
        self.assertIn(target.read_text(encoding="utf-8"), payloads, "the published page is torn")
        self.assertEqual(list(d.glob("*.tmp")), [], "temp files must not be left behind")

    def test_temp_names_are_unique_per_write(self):
        """The guarantee behind the test above: no two writers share a path."""
        from hermes_dashboard import build as bld
        d = Path(tempfile.mkdtemp(prefix="hd-tmpname-"))
        target = d / "index.html"
        names = []
        real = bld.tempfile.mkstemp

        def spy(*a, **kw):
            fd, name = real(*a, **kw)
            names.append(name)
            return fd, name

        bld.tempfile.mkstemp = spy
        try:
            bld._atomic_write(target, "x")
            bld._atomic_write(target, "y")
        finally:
            bld.tempfile.mkstemp = real
        self.assertEqual(len(set(names)), 2, "temp paths must differ between writes")
        for n in names:
            self.assertNotEqual(n, str(target) + ".tmp")

    def test_build_lock_keeps_a_second_build_out(self):
        from hermes_dashboard import build as bld
        d = Path(tempfile.mkdtemp(prefix="hd-lock-"))
        first = bld._BuildLock(d).__enter__()
        self.assertTrue(first.held)
        second = bld._BuildLock(d).__enter__()
        self.assertFalse(second.held)
        first.__exit__(None, None, None)
        third = bld._BuildLock(d).__enter__()
        self.assertTrue(third.held, "the lock must be released, not leaked")
        third.__exit__(None, None, None)

    def test_validate_rejects_values_that_would_crash_the_build(self):
        self.assertTrue(any("offset_hours" in e for e in
                            config.Config({"timezone": {"offset_hours": 24}}).validate()))
        self.assertEqual(config.Config({"timezone": {"offset_hours": -11}}).validate(), [])
        self.assertTrue(any("stt_log_regex" in e for e in
                            config.Config({"usage": {"stt_log_regex": "["}}).validate()))
        self.assertTrue(any("tirith_regex" in e for e in
                            config.Config({"security": {"tirith_regex": "(unclosed"}}).validate()))

    def test_a_broken_section_costs_only_that_section(self):
        from hermes_dashboard import build as bld

        def boom():
            raise RuntimeError("nope")

        self.assertEqual(bld._safe("x", boom), "")
        self.assertEqual(bld._safe("x", boom, ("", "")), ("", ""))
        self.assertEqual(bld._safe("x", lambda: "fine"), "fine")

    def test_passive_sessions_are_not_cost_and_not_fallback(self):
        home = make_home()
        os.environ["HERMES_HOME"] = str(home)
        config.set_current(config.Config(PROVIDERS))
        db = sqlite3.connect(home / "state.db")
        # a paid provider row that never called the model
        db.execute("INSERT INTO sessions (id, source, model, started_at, billing_provider, "
                   "api_call_count) VALUES ('p1','telegram','claude-sonnet-5',?, 'anthropic', 0)",
                   (time.time() - 100,))
        db.commit()
        db.close()
        n = common.scalar("SELECT count(*) FROM sessions WHERE " + common.fallback())
        self.assertEqual(n, 1, "a session with no model call is not a forced fallback")
        d = common.scalar("SELECT count(*) FROM sessions WHERE " + common.deliberate_paid())
        self.assertEqual(d, 1, "only the CLI run is deliberate paid work")

    def test_web_fonts_can_be_turned_off(self):
        from hermes_dashboard import render
        on = config.Config({})
        off = config.Config({"views": {"web_fonts": False}})
        self.assertIn("fonts.googleapis.com", render.fonts(on))
        self.assertNotIn("googleapis", render.fonts(off))
        self.assertIn("--f-body", render.fonts(off), "system stack must replace the web fonts")

    def test_csv_upload_lands_where_the_collector_reads(self):
        from hermes_dashboard import settings as st
        home = Path(tempfile.mkdtemp(prefix="hd-csv-"))
        (home / "cache").mkdir()
        cfg = config.Config({"providers": {"paid": [{"id": "anthropic", "cost_cache": "cache/x.json"}]}})
        cfg.home = home
        state = st.State(cfg)
        # the name the collector globs for
        self.assertTrue(state.csv_path().name.startswith("anthropic_"))
        os.environ["HERMES_DASHBOARD_ANTHROPIC_COST_CSV"] = str(home / "explicit.csv")
        try:
            self.assertEqual(state.csv_path(), home / "explicit.csv")
        finally:
            del os.environ["HERMES_DASHBOARD_ANTHROPIC_COST_CSV"]


class BuildTests(unittest.TestCase):
    def _build(self, home: Path, extra: dict | None = None) -> dict[str, str]:
        cfgp = home / "dashboard" / "dashboard.json"
        cfgp.parent.mkdir(exist_ok=True)
        data = {"i18n": {"default": "en", "languages": ["en", "ru"]}, "views": {"config_map": False},
                "agent": {"name": "TestAgent", "people": [{"label": "Alice", "handle": "@alice"}]},
                "chats": {"names": {"-100": "Family chat"}},
                "cron": {"dream_job_id": "aaa", "watchdogs": [{"id": "bbb", "label": "WD", "schedule": "every 15 min"}]},
                "security": {"shield_bullets": {"en": ["rule A"]}}}
        data.update(json.loads(json.dumps(PROVIDERS)))
        data.update(extra or {})
        cfgp.write_text(json.dumps(data), encoding="utf-8")
        out = home / "public"
        env = dict(os.environ, HERMES_HOME=str(home), PYTHONPATH=str(ROOT))
        r = subprocess.run([sys.executable, "-m", "hermes_dashboard.build", "--config", str(cfgp), "--out", str(out)],
                           env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        return {p.name: p.read_text(encoding="utf-8") for p in out.glob("*.html")}

    def test_empty_home_builds(self):
        home = Path(tempfile.mkdtemp(prefix="hd-empty-"))
        pages = self._build(home)
        self.assertIn("index.html", pages)
        self.assertIn("connectors.html", pages)
        for name, html in pages.items():
            self.assertEqual(html.count("<div"), html.count("</div>"), f"div balance in {name}")

    def test_missing_venv_and_budgets_env_do_not_break_the_build(self):
        # A fresh install: paths.venv_python points nowhere and the quota cache is
        # configured. Collectors must fail open, and budgets.env must feed the bar
        # when the config number is 0.
        home = make_home()
        (home / "dashboard").mkdir(exist_ok=True)
        (home / "dashboard" / "budgets.env").write_text(
            "# ref\nCODEX_WEEKLY_INPUT_BUDGET=7000000  # tokens\nCODEX_SUB_USD_MO=20\n", encoding="utf-8")
        pages = self._build(home, {"paths": {"venv_python": str(home / "no" / "such" / "python")},
                                   "providers": {"primary": {"id": "openai-codex", "label": "Codex",
                                                             "billing": "subscription", "weekly_input_budget": 0,
                                                             "quota_cache": "cache/codex_quota.json"}}})
        self.assertIn("index.html", pages)
        self.assertIn("7.0M", pages["index.html"])   # weekly bar scaled from budgets.env

    def test_favicon_is_inlined_and_optional(self):
        home = make_home()
        pages = self._build(home)
        self.assertIn('<link rel="icon" type="image/x-icon" href="data:image/x-icon;base64,',
                      pages["index.html"])
        # a missing or empty favicon must produce no <link> at all, not a broken one
        from hermes_dashboard import config as cfgmod, render
        self.assertEqual(render.favicon_link(cfgmod.Config({"agent": {"favicon": ""}})), "")
        self.assertEqual(render.favicon_link(cfgmod.Config({"agent": {"favicon": "no/such.ico"}})), "")

    def test_synthetic_db(self):
        home = make_home()
        pages = self._build(home)
        en, ru = pages["index.html"], pages["index.ru.html"]
        self.assertIn("Sessions · 7 days", en)
        self.assertIn("Сессий · 7 дней", ru)
        # KPI: 5 active sessions, 1 passive, 1 forced fallback
        self.assertRegex(en, r"Sessions · 7 days</div><div class=\"v\">5<")
        self.assertIn("no answer: 1", re.sub(r"<[^>]+>", "", en))
        self.assertRegex(en, r"Fallback · 7 days</div><div class=\"v\">1<")
        self.assertIn("Family chat", en)
        self.assertIn("boom", en)          # failing cron in the events feed
        self.assertIn("Alice", en)
        # cost: core estimate is present for both anthropic sessions → labelled as core estimate
        self.assertIn("Hermes core estimate", en)
        for name, html in pages.items():
            self.assertEqual(html.count("<div"), html.count("</div>"), f"div balance in {name}")
        # the English page must not contain Cyrillic outside agent data (none in this fixture)
        stripped = re.sub(r"<style>.*?</style>", "", en, flags=re.S)
        self.assertIsNone(re.search(r"[А-Яа-яЁё]", stripped), "Cyrillic leaked into the English page")
        # language switch present and pointing at the other file
        self.assertIn('href="index.ru.html"', en)
        self.assertIn('href="index.html"', ru)


class SettingsTests(unittest.TestCase):
    def _state(self, cfg_data=None):
        from hermes_dashboard import settings
        home = make_home(with_db=False)
        cfgp = home / "dashboard" / "dashboard.json"
        cfgp.parent.mkdir()
        cfgp.write_text(json.dumps(cfg_data or {}), encoding="utf-8")
        os.environ["HERMES_HOME"] = str(home)
        return settings, settings.State(config.load_config(str(cfgp))), home, cfgp

    def test_raw_json_validation_and_backup(self):
        settings, st, home, cfgp = self._state()
        self.assertIsNotNone(st.save_config_raw("{not json"))
        self.assertIsNotNone(st.save_config_raw('{"providers": {"paid": [{"id": "x", "in_per_m": "abc"}]}}'))
        self.assertIsNone(st.save_config_raw('{"agent": {"name": "X"}}'))
        self.assertTrue(cfgp.with_suffix(".json.bak").is_file())

    def test_form_writes_only_declared_paths(self):
        settings, st, home, cfgp = self._state({"agent": {"name": "Old"}, "keep": {"me": 1}})
        form = {
            "agent.name": "New name",
            "providers.primary.subscription_usd_month": "20",
            "providers.cost_fresh_days": "6",
            "views.config_map": "0",
            "providers.fallback_sources": "telegram, cron",
            "agent.tagline.en": "Hello",
            "chats.names.__k.__present": "1",
            "chats.names.__k.0": "-100777", "chats.names.__v.0": "Family",
            "chats.names.__k.1": "", "chats.names.__v.1": "dropped",
            "providers.paid.id.__present": "1",
            "providers.paid.id.0": "anthropic", "providers.paid.label.0": "Claude",
            "providers.paid.in_per_m.0": "3", "providers.paid.out_per_m.0": "15",
            "providers.paid.exclude_models.0": "a-model, b-model",
            "providers.paid.id.1": "", "providers.paid.label.1": "ghost row",
            "evil.injected": "nope",
        }
        self.assertIsNone(st.save_settings(form))
        saved = json.loads(cfgp.read_text(encoding="utf-8"))
        self.assertEqual(saved["agent"]["name"], "New name")
        self.assertEqual(saved["keep"], {"me": 1}, "untouched keys must survive")
        self.assertNotIn("evil", saved, "only schema paths may be written")
        self.assertEqual(saved["providers"]["primary"]["subscription_usd_month"], 20.0)
        self.assertIs(saved["views"]["config_map"], False)
        self.assertEqual(saved["providers"]["fallback_sources"], ["telegram", "cron"])
        self.assertEqual(saved["chats"]["names"], {"-100777": "Family"}, "row without a key is dropped")
        self.assertEqual(len(saved["providers"]["paid"]), 1, "row without the primary field is dropped")
        self.assertEqual(saved["providers"]["paid"][0]["exclude_models"], ["a-model", "b-model"])
        self.assertEqual(saved["agent"]["tagline"]["en"], "Hello")

    def test_number_field_rejects_text(self):
        settings, st, home, cfgp = self._state()
        err = st.save_settings({"providers.primary.subscription_usd_month": "twenty"})
        self.assertIsNotNone(err)
        self.assertIn("subscription_usd_month", err)

    def test_budget_file_keeps_comments(self):
        settings, st, home, cfgp = self._state()
        bp = st.budgets_path()
        bp.parent.mkdir(parents=True, exist_ok=True)
        bp.write_text("# header comment\n\nA_LIMIT=100   # inline note\nB_LIMIT=5\n", encoding="utf-8")
        self.assertIsNone(st.save_budgets({"budget.A_LIMIT": "250", "budget.B_LIMIT": "5",
                                           "budget.__newkey": "C_LIMIT", "budget.__newval": "7"}))
        text = bp.read_text(encoding="utf-8")
        self.assertIn("# header comment", text)
        self.assertIn("A_LIMIT=250", text)
        self.assertIn("# inline note", text)
        self.assertIn("C_LIMIT=7", text)
        self.assertTrue(bp.with_suffix(".env.bak").is_file())
        self.assertIsNotNone(st.save_budgets({"budget.__newkey": "bad name", "budget.__newval": "1"}))

    def test_csv_upload_guard(self):
        settings, st, home, cfgp = self._state()
        self.assertIsNotNone(st.save_csv(b"hello world", "x.csv"))
        self.assertIsNone(st.save_csv(b"usage_date_utc,cost_usd\n2026-01-01,1\n", "x.csv"))
        self.assertTrue(st.csv_path().is_file())

    def test_page_renders_in_the_design_system(self):
        settings, st, home, cfgp = self._state({"i18n": {"default": "en", "languages": ["en", "ru"]}})
        i18n.set_lang("en")
        host = {"dot": "var(--ok)", "gwt": "running", "sync": "-", "sha": "abc", "commit": "abc x"}
        page = settings.build_page(st, "en", host)
        for marker in ('class="rail"', 'class="work"', 'class="kpis prime"', 'class="fset"',
                       'name="_csrf"', 'class="segs"', "Save this section"):
            self.assertIn(marker, page, marker)
        self.assertEqual(page.count("<div"), page.count("</div>"), "div balance")
        self.assertIn("<style>", page, "the page must carry the dashboard stylesheet")


class HygieneTests(unittest.TestCase):
    """The public repo must not carry identifiers of any real installation.

    The patterns describe *classes* of leaks (chat ids, key literals, absolute
    home paths) rather than the concrete strings of one install — a scanner that
    hardcodes the very identifiers it forbids would leak them itself. To check a
    specific install too, pass the literals in:

        HERMES_DASHBOARD_SCAN_EXTRA="my-host,my-repo,@myhandle" bin/hermes-dashboard check
    """

    CLASSES = {
        "telegram chat id": r"-100\d{9,}",
        "long bare id": r"(?<![\d.])\d{9,}(?![\d.])",
        "api key literal": (r"apikey_[A-Za-z0-9]{8,}|sk-[A-Za-z0-9]{16,}"
                            r"|ghp_[A-Za-z0-9]{16,}|xox[baprs]-[A-Za-z0-9-]{10,}"),
        "absolute home path": r"/home/[a-z][a-z0-9_-]*/|[A-Za-z]:\\Users\\",
        "private config repo": r"[a-z0-9]+-hermes-config",
    }
    SCAN_SUFFIXES = (".py", ".md", ".json", ".css", ".env", ".conf", ".service", ".sh", "")

    def _files(self):
        for p in ROOT.rglob("*"):
            if not p.is_file() or ".git" in p.parts or "__pycache__" in p.parts:
                continue
            if p.suffix in self.SCAN_SUFFIXES:
                yield p

    def test_no_installation_identifiers(self):
        patterns = dict(self.CLASSES)
        for lit in [x.strip() for x in os.environ.get("HERMES_DASHBOARD_SCAN_EXTRA", "").split(",") if x.strip()]:
            # word-bounded and case-sensitive: a bare substring match turns short names
            # into false positives (a Russian name inside an ordinary word, say)
            patterns["extra:" + lit] = r"(?<!\w)" + re.escape(lit) + r"(?!\w)"
        hits = []
        for p in self._files():
            txt = p.read_text(encoding="utf-8", errors="ignore")
            for label, rx in patterns.items():
                for m in re.finditer(rx, txt):
                    line = txt[:m.start()].count("\n") + 1
                    hits.append("{}:{} [{}] {}".format(p.relative_to(ROOT), line, label, m.group(0)[:40]))
        self.assertEqual(hits, [], "identifiers of a real installation found:\n" + "\n".join(hits))

    def test_english_ui_strings(self):
        """UI text lives in English _() keys; other languages belong to locales/."""
        offenders = []
        for p in (ROOT / "hermes_dashboard").rglob("*.py"):
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if not re.search(r"[Ѐ-ӿ]", line):
                    continue
                # DEFAULTS ship bundled ru translations of a few strings, and the cron
                # keyword lists match job names in any language — both are data, not UI.
                if re.search(r'"(ru|en)":|keywords', line):
                    continue
                offenders.append("{}:{}: {}".format(p.relative_to(ROOT), i, line.strip()[:90]))
        self.assertEqual(offenders, [], "non-English text outside locales:\n" + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
