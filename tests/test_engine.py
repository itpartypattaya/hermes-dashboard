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

    def test_set_lang_can_enforce_configured_allowlist(self):
        self.assertEqual(i18n.set_lang('" onmouseover="x', ["en", "ru"], "en"), "en")
        self.assertEqual(i18n.lang(), "en")
        self.assertEqual(i18n.set_lang("ru", ["en", "ru"], "en"), "ru")

    def test_settings_post_redirect_allowlists_and_encodes_lang(self):
        from io import BytesIO
        from hermes_dashboard import settings as st

        cfg = config.Config({"i18n": {"default": "en", "languages": ["en", "ru", "日本"]}})
        state = type("S", (), {"cfg": cfg, "token": "csrf", "lock": __import__("threading").Lock(),
                                "messages": [], "flash": lambda self, kind, text: None})()
        h = st.Handler.__new__(st.Handler)
        h.state, h.path = state, "/settings/"  # type: ignore[assignment]
        h.headers = {"Content-Length": "0", "Content-Type": "application/x-www-form-urlencoded",  # type: ignore[assignment]
                     "Origin": "http://localhost", "Host": "localhost"}
        h.rfile = BytesIO(b"_csrf=csrf&action=unknown&lang=%0d%0aX-Evil%3A%201")
        h.headers["Content-Length"] = str(len(h.rfile.getvalue()))
        sent = []
        h.send_response = lambda code: sent.append(("status", code))  # type: ignore[assignment]
        h.send_header = lambda key, value: sent.append((key, value))  # type: ignore[assignment]
        h.end_headers = lambda: None
        h._send = lambda *args, **kwargs: sent.append(("body", args[0] if args else ""))
        h._do_post_locked()
        location = dict((k, v) for k, v in sent if k == "Location")["Location"]
        self.assertEqual(location, "/settings/?lang=en")
        self.assertNotIn("\r", location)
        self.assertNotIn("\n", location)
        self.assertEqual([k for k, _ in sent if k not in ("status", "body")], ["Location"])

        h.headers["Origin"] = "https://attacker.example"
        sent.clear()
        h.rfile = BytesIO(b"_csrf=csrf&action=unknown&lang=ru")
        h.headers["Content-Length"] = str(len(h.rfile.getvalue()))
        h._do_post_locked()
        self.assertEqual(sent, [("body", "Request rejected: bad or missing CSRF token.")])
        h.headers["Origin"] = "http://localhost"

        # Exercise both parser representations: a percent-encoded CRLF becomes
        # a decoded value before the handler sees it, while raw CRLF is already
        # decoded in a hand-built request body. Neither may reach a header.
        for payload in (b"%0d%0aX-Evil%3a%201", b"\r\nX-Evil: 1"):
            h.rfile = BytesIO(b"_csrf=csrf&action=unknown&lang=" + payload)
            h.headers["Content-Length"] = str(len(h.rfile.getvalue()))
            sent.clear()
            h._do_post_locked()
            headers = [(k, v) for k, v in sent if k not in ("status", "body")]
            self.assertEqual(headers, [("Location", "/settings/?lang=en")])
            self.assertNotRegex(headers[0][1], r"[\r\n]")

        h.rfile = BytesIO(b"_csrf=csrf&action=unknown&lang=%E6%97%A5%E6%9C%AC")
        h.headers["Content-Length"] = str(len(h.rfile.getvalue()))
        sent.clear()
        h._do_post_locked()
        location = dict((k, v) for k, v in sent if k == "Location")["Location"]
        self.assertEqual(location, "/settings/?lang=%E6%97%A5%E6%9C%AC")

        for payload in (b'" onmouseover="alert(1)', b'<script>alert(1)</script>', b'unknown', b''):
            h.rfile = BytesIO(b"_csrf=csrf&action=unknown&lang=" + payload.replace(b" ", b"%20"))
            h.headers["Content-Length"] = str(len(h.rfile.getvalue()))
            sent.clear()
            h._do_post_locked()
            location = dict((k, v) for k, v in sent if k == "Location")["Location"]
            self.assertEqual(location, "/settings/?lang=en")
            self.assertEqual(i18n.lang(), "en")

        h.rfile = BytesIO(b"_csrf=csrf&action=unknown&lang=ru")
        h.headers["Content-Length"] = str(len(h.rfile.getvalue()))
        sent.clear()
        h._do_post_locked()
        self.assertEqual(dict((k, v) for k, v in sent if k == "Location")["Location"], "/settings/?lang=ru")

    def test_settings_get_keeps_language_in_state_cookie_and_form_safe(self):
        """GET must apply the same allowlist to every reflected lang sink."""
        from hermes_dashboard import settings as st

        cfg = config.Config({"i18n": {"default": "en", "languages": ["en", "ru", "日本"]}})
        state = type("S", (), {"cfg": cfg})()
        h = st.Handler.__new__(st.Handler)
        h.state = state  # type: ignore[assignment]
        h.path = "/settings/?lang=%22%20onfocus%3D%22alert(1)%0d%0aX"
        h.headers = {"Host": "localhost"}  # type: ignore[assignment]
        captured = {}
        original_build_page = st.build_page

        def fake_build_page(_state, lang, host):
            captured["page"] = (lang, host)
            return "<safe>"

        st.build_page = fake_build_page
        h._host_info = lambda: {}
        h._send = lambda body, status=200, ctype="text/html; charset=utf-8", extra=None: captured.update(
            body=body, status=status, ctype=ctype, extra=extra or {})
        try:
            h._do_get_locked("/settings/")
        finally:
            st.build_page = original_build_page
        self.assertEqual(captured["page"][0], "en")
        self.assertEqual(captured["status"], 200)
        self.assertEqual(captured["ctype"], "text/html; charset=utf-8")
        self.assertEqual(captured["extra"], {"Set-Cookie": "hd-lang=en; Path=/; HttpOnly; SameSite=Lax"})
        self.assertNotIn("X-Evil", captured["extra"]["Set-Cookie"])

    def test_security_headers_and_https_cookie_flag(self):
        from io import BytesIO
        from hermes_dashboard import settings as st

        h = st.Handler.__new__(st.Handler)
        sent = []
        h.send_response = lambda code: sent.append(("status", code))
        h.send_header = lambda key, value: sent.append((key, value))
        h.end_headers = lambda: None
        h.wfile = BytesIO()
        h._send("ok")
        headers = dict((k, v) for k, v in sent if k not in ("status",))
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertIn("default-src 'none'", headers["Content-Security-Policy"])
        self.assertIn("script-src 'self' 'unsafe-inline'", headers["Content-Security-Policy"])

        h.headers = {"X-Forwarded-Proto": "http"}
        self.assertFalse(h._https_request())
        h.headers["X-Forwarded-Proto"] = "https, http"
        self.assertTrue(h._https_request())

    def test_csrf_hidden_fields_escape_html_attributes(self):
        from hermes_dashboard import settings as st

        state = type("S", (), {"token": 'csrf\"&<',})()
        markup = st._csrf(state, '" onfocus="alert(1)', '<script>')  # type: ignore[arg-type]
        self.assertNotIn('csrf"&<', markup)
        self.assertIn("csrf&quot;&amp;&lt;", markup)
        self.assertIn("&quot; onfocus=&quot;alert(1)", markup)
        self.assertIn("&lt;script&gt;", markup)

    def test_post_rejects_invalid_content_length_and_ipv6_origin_mismatch(self):
        from io import BytesIO
        from hermes_dashboard import settings as st

        state = type("S", (), {"cfg": config.Config({}), "token": "csrf"})()
        h = st.Handler.__new__(st.Handler)
        h.state, h.path = state, "/settings/"
        h.rfile = BytesIO(b"")
        sent = []
        h.send_response = lambda code: sent.append(("status", code))
        h.send_header = lambda key, value: sent.append((key, value))
        h.end_headers = lambda: None
        h.wfile = BytesIO()
        h._send = lambda body, status=200, ctype="text/html; charset=utf-8", extra=None: sent.append(("body", status))

        for value in ("not-a-number", "-1"):
            h.headers = {"Content-Length": value}
            sent.clear()
            h._do_post_locked()
            self.assertEqual(sent, [("body", 400)])

        h.headers = {"Content-Length": "0", "Origin": "http://[::1]", "Host": "[::2]"}
        self.assertFalse(h._same_host(), "different IPv6 hosts must not pass same-host validation")

    def test_every_writer_uses_a_unique_temp_name(self):
        """The race fixed in build.py also lived in settings, history and both caches."""
        import inspect
        from hermes_dashboard import history, settings as st
        from hermes_dashboard.collectors import anthropic_cost, codex_quota
        for mod in (st, history, anthropic_cost, codex_quota):
            src = inspect.getsource(mod)
            self.assertNotIn('with_suffix(".tmp")', src,
                             f"{mod.__name__} still derives its temp name from the target")

    def test_atomic_write_handles_text_and_bytes(self):
        d = Path(tempfile.mkdtemp(prefix="hd-aw-"))
        common.atomic_write(d / "a.txt", "hello")
        common.atomic_write(d / "b.bin", b"\x00\x01")
        self.assertEqual((d / "a.txt").read_text(encoding="utf-8"), "hello")
        self.assertEqual((d / "b.bin").read_bytes(), b"\x00\x01")
        self.assertEqual(list(d.glob("*.tmp")), [])
        # writes into a directory that does not exist yet
        common.atomic_write(d / "sub" / "c.txt", "x")
        self.assertEqual((d / "sub" / "c.txt").read_text(encoding="utf-8"), "x")

    def test_child_environment_drops_secrets_but_keeps_the_platform(self):
        """Rework of PR #2: a denylist, so an unknown host still works.

        A strict allowlist broke TLS trust stores, proxies and Windows spawning;
        the point here is only to stop handing the agent's credentials to our
        own subprocesses.
        """
        saved = dict(os.environ)
        try:
            os.environ.update({
                "ANTHROPIC_ADMIN_KEY": "sk-admin", "TELEGRAM_BOT_TOKEN": "t",
                "AWS_SECRET_ACCESS_KEY": "a", "SSH_AUTH_SOCK": "/tmp/s",
                "GITHUB_TOKEN": "g", "SSL_CERT_FILE": "/etc/ssl/cert.pem",
                "HTTPS_PROXY": "http://proxy:3128", "PATH": os.environ.get("PATH", "/usr/bin"),
            })
            env = config.child_environment(Path("/tmp/home"), {"PYTHONPATH": "/engine"})
            for gone in ("TELEGRAM_BOT_TOKEN", "AWS_SECRET_ACCESS_KEY", "SSH_AUTH_SOCK",
                         "GITHUB_TOKEN"):
                self.assertNotIn(gone, env, f"{gone} must not reach a child process")
            for kept in ("PATH", "SSL_CERT_FILE", "HTTPS_PROXY"):
                self.assertIn(kept, env, f"{kept} is needed for the child to work at all")
            self.assertEqual(env["ANTHROPIC_ADMIN_KEY"], "sk-admin",
                             "the collector documents that it reads this one")
            self.assertEqual(env["HERMES_HOME"], str(Path("/tmp/home")))
            self.assertEqual(env["PYTHONPATH"], "/engine")
            self.assertNotIn("HOME", config._CHILD_ENV_KEEP,
                             "HOME must never be hardcoded to a packager's path")
        finally:
            os.environ.clear()
            os.environ.update(saved)

    def test_out_dir_outside_the_agent_home_stays_legal(self):
        """A webroot is never inside ~/.hermes — confining it breaks deployment."""
        cfg = config.Config({"paths": {"hermes_home": "/srv/agent/.hermes",
                                       "out_dir": "/var/www/dashboard"}})
        self.assertEqual(cfg.validate(), [])
        self.assertEqual(str(cfg.out_dir).replace("\\", "/"), "/var/www/dashboard")

    def test_provider_error_keeps_the_message_not_the_body(self):
        import io as _io
        import urllib.error
        from hermes_dashboard.collectors import anthropic_cost as ac

        def err(payload: bytes):
            return urllib.error.HTTPError("u", 400, "Bad Request", {}, _io.BytesIO(payload))

        msg = ac._error_message(err(b'{"error":{"message":"limit: 32 is greater than 31"}}'))
        self.assertEqual(msg, "limit: 32 is greater than 31")
        self.assertEqual(ac._error_message(err(b"<html>oops</html>")), "(unparseable error body)")
        self.assertEqual(ac._error_message(err(b'{"error":{}}')), "(no message in the error body)")

    # ── fourth sweep: one concept, one implementation ──────────────────────

    def test_an_uploaded_export_is_found_whatever_the_provider_is_called(self):
        """The write side and the read side must share one filename convention.

        Settings wrote "<id>_cost_export.csv" while the collector globbed for
        "anthropic_*" and "claude_api_cost*": for any other provider id the
        upload was saved, reported as saved, and never read again.
        """
        import fnmatch
        from hermes_dashboard.collectors.anthropic_cost import CSV_GLOBS, CSV_SUFFIX
        from hermes_dashboard import settings as st
        self.assertEqual(st.CSV_NAME, CSV_SUFFIX, "settings must not invent its own name")
        for pid in ("anthropic", "claude", "openai", "vertex", "my-provider"):
            written = pid + "_" + CSV_SUFFIX
            self.assertTrue(any(fnmatch.fnmatch(written, g) for g in CSV_GLOBS),
                            f"an upload for {pid!r} would never be read")

    def test_a_language_without_a_locale_is_reported(self):
        """Two sources for "what languages exist": the config list and the files.

        Unreconciled, the build wrote index.de.html full of English and labelled
        it <html lang="de">, with the switcher offering the translation.
        """
        errs = config.Config({"i18n": {"default": "en", "languages": ["en", "de"]}}).validate()
        self.assertTrue(any("locales/de.json" in e for e in errs))
        # en never has a file of its own, and a real locale must stay silent
        self.assertEqual(config.Config({"i18n": {"default": "ru",
                                                 "languages": ["ru", "en"]}}).validate(), [])

    def test_the_per_provider_fallback_follows_the_shared_rule(self):
        """gen_usage built this condition by hand and so missed two later fixes:
        the activity rule (v0.4.0) and SQL quoting (v0.5.1)."""
        cfg = config.Config({"providers": {
            "paid": [{"id": "anthropic"}],
            "fallback_sources": ["telegram", "o'clock"]}})
        config.set_current(cfg)
        cond = common.forced_fallback_of(common.provider_cond({"id": "anthropic"}), cfg)
        self.assertIn(common.active(), cond, "a session with no model call is not a fallback")
        self.assertIn("'o''clock'", cond, "source names must be quoted like every other literal")
        # and it is really valid SQL
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE sessions(billing_provider TEXT, model TEXT, source TEXT,"
                   " api_call_count INT)")
        db.execute("INSERT INTO sessions VALUES ('anthropic','m','telegram',0)")
        db.execute("INSERT INTO sessions VALUES ('anthropic','m','telegram',2)")
        self.assertEqual(db.execute("SELECT count(*) FROM sessions WHERE " + cond).fetchone()[0], 1)

    def test_free_is_decided_the_same_way_in_python_and_sql(self):
        """A free entry with no `model` covers every model of that provider in
        SQL; the cron billing tiers used an exact (id, model) pair instead."""
        cfg = config.Config({"providers": {"free": [{"id": "gemini"}]}})
        config.set_current(cfg)
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE s(billing_provider TEXT, model TEXT)")
        rows = [("gemini", "gemini-2.5-flash"), ("gemini", "gemini-3.0"),
                ("gemini", None), ("anthropic", "x")]
        db.executemany("INSERT INTO s VALUES (?,?)", rows)
        in_sql = {(r[0], r[1]) for r in
                  db.execute("SELECT billing_provider, model FROM s WHERE " + common.free_cond())}
        for prov, model in rows:
            self.assertEqual(common.is_free_row(prov or "", model or "", cfg),
                             (prov, model) in in_sql, f"twins disagree on {prov!r}/{model!r}")

    def test_sql_string_quoting_has_one_implementation(self):
        """gen_banner carried its own copy of _q(); gen_usage carried none."""
        import inspect
        from hermes_dashboard import gen_banner, gen_usage
        for mod in (gen_banner, gen_usage):
            src = inspect.getsource(mod)
            self.assertNotIn('replace("\'", "\'\'")', src,
                             f"{mod.__name__} re-implements SQL quoting")
            self.assertNotIn("f\"'{s}'\"", src, f"{mod.__name__} interpolates a bare literal")
        config.set_current(config.Config({"providers": {"fallback_sources": ["a'b"]}}))
        self.assertIn("'a''b'", common.src_in())

    def test_the_language_charset_is_defined_once(self):
        import inspect
        from hermes_dashboard import settings as st
        self.assertNotIn("A-Za-z0-9_-", inspect.getsource(st),
                         "settings must take the charset from config, not repeat it")
        self.assertEqual(st.LANG_CHARS, config.LANG_CHARS)

    # ── third sweep ────────────────────────────────────────────────────────

    def test_one_definition_of_which_day(self):
        """SQLite 'localtime' is the machine's zone, tz() is the configured one.

        On a VPS running UTC with offset_hours=7, everything before 07:00 fell
        into the previous day on the daily chart while the event feed showed the
        right one — two answers to the same question on one page.
        """
        from datetime import datetime, timedelta, timezone as _tz
        config.set_current(config.Config({"timezone": {"offset_hours": 7}}))
        cfg_tz = _tz(timedelta(hours=7))
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE s(started_at REAL)")
        for hour in (0, 2, 6, 12, 23):
            e = datetime(2026, 8, 30, hour, 0, tzinfo=cfg_tz).timestamp()
            db.execute("DELETE FROM s")
            db.execute("INSERT INTO s VALUES (?)", (e,))
            in_sql = db.execute("SELECT " + common.local_day() + " FROM s").fetchone()[0]
            in_feed = datetime.fromtimestamp(e, cfg_tz).strftime("%Y-%m-%d")
            self.assertEqual(in_sql, in_feed, f"chart and feed disagree for {hour:02d}:00")
        self.assertNotIn("localtime", common.local_day())

    def test_the_label_of_a_free_row_is_not_the_paid_one(self):
        """paid_label() matched on provider id alone while its two twins matched
        on the model too — one id can carry a paid slot and a free key."""
        config.set_current(config.Config({"providers": {
            "paid": [{"id": "gemini", "label": "PAID", "model_like": "gemini-%",
                      "exclude_models": ["gemini-2.5-flash"]}],
            "free": [{"id": "gemini", "model": "gemini-2.5-flash", "label": "FREE"}]}}))
        self.assertEqual(common.paid_label("gemini", "gemini-3.0-pro"), "PAID")
        self.assertEqual(common.paid_label("gemini", "gemini-2.5-flash"), "FREE")
        self.assertEqual(common.paid_label("nobody", "some-model"), "some-model")

    def test_zero_is_rendered_not_swallowed(self):
        """esc() used `str(s or "")`, so a legitimate zero rendered as blank."""
        self.assertEqual(common.esc(0), "0")
        self.assertEqual(common.esc(0.0), "0.0")
        self.assertEqual(common.esc(None), "")
        self.assertEqual(common.esc("<b>"), "&lt;b&gt;")
        self.assertIn(">0<", common.simple_bar("l", 0, 100, "c", value_txt=0))

    # ── second sweep: classes not covered by the first ─────────────────────

    def test_read_text_safe_never_raises_on_content(self):
        d = Path(tempfile.mkdtemp(prefix="hd-dec-"))
        bad = d / "bad.md"
        bad.write_bytes(b"a\xff\xfeb")
        self.assertIn("a", common.read_text_safe(bad))
        self.assertEqual(common.read_text_safe(d / "missing.md", "dflt"), "dflt")

    def test_a_failing_probe_costs_only_its_own_value(self):
        """_safe() was applied to five call sites; the rest could kill the build."""
        import inspect
        from hermes_dashboard import build as bld
        src = inspect.getsource(bld._build_pages)
        for step in ("collect_kpis", "memory_facts", "gen_banner.build", "gen_banner.state",
                     "sysinfo.git_head", "view_overview", "history.update_and_deltas"):
            idx = src.find(step)
            self.assertGreater(idx, 0, f"{step} no longer runs in _build_pages")
            window = src[max(0, idx - 200):idx]
            self.assertIn("_safe(", window, f"{step} is not contained by _safe()")

    def test_the_build_lock_is_released_when_a_build_raises(self):
        """__enter__/__exit__ called by hand leaked the lock on any exception."""
        import inspect
        from hermes_dashboard import build as bld
        src = inspect.getsource(bld.build_all)
        self.assertIn("with _BuildLock(", src)
        self.assertNotIn(".__enter__()", src, "the lock must be held by `with`, not by hand")
        self.assertNotIn(".__exit__(", inspect.getsource(bld._build_pages))

    def test_python_and_sql_agree_on_what_is_paid(self):
        """fnmatch folds case only where the filesystem does; SQL LIKE always does.

        The twins therefore agreed on Windows and disagreed on Linux — the
        routing banner and the cost card would contradict each other.
        """
        cfg = config.Config({"providers": {"paid": [
            {"id": "gemini", "model_like": "gemini-%", "exclude_models": ["gemini-2.5-flash"]},
            {"id": "anthropic"}]}})
        config.set_current(cfg)
        db = sqlite3.connect(":memory:")
        db.execute("CREATE TABLE sessions(billing_provider TEXT, model TEXT)")
        rows = [(p, m) for p in ("gemini", "anthropic", "openai-codex", "")
                for m in ("gemini-3.0-pro", "GEMINI-3.0", "Gemini-3.0", "gemini-2.5-flash",
                          "claude-x", "gpt-5.5", "")]
        db.executemany("INSERT INTO sessions VALUES (?,?)", rows)
        in_sql = {(r[0], r[1]) for r in
                  db.execute("SELECT billing_provider, model FROM sessions WHERE " + common.paid())}
        for prov, model in rows:
            self.assertEqual(common.is_paid_row(prov, model), (prov, model) in in_sql,
                             f"twins disagree on {prov!r}/{model!r}")

    def test_every_numeric_setting_is_validated_and_degrades(self):
        bad = config.Config({"providers": {"primary": {"subscription_usd_month": "20$",
                                                       "weekly_input_budget": "lots",
                                                       "ref_in_per_m": "x", "ref_out_per_m": "y"},
                                           "cost_fresh_days": "six"},
                             "memory": {"memory_char_limit_default": "many",
                                        "user_char_limit_default": "some"}})
        errs = bad.validate()
        for key in ("subscription_usd_month", "weekly_input_budget", "ref_in_per_m",
                    "ref_out_per_m", "cost_fresh_days", "memory_char_limit_default",
                    "user_char_limit_default"):
            self.assertTrue(any(key in e for e in errs), f"{key} is not validated")
        # and the read path falls back rather than raising
        self.assertEqual(bad.number("providers.cost_fresh_days", 6, int), 6)
        self.assertEqual(bad.number("providers.primary.subscription_usd_month", 0.0), 0.0)
        self.assertEqual(config.Config({}).number("providers.cost_fresh_days", 6, int), 6)

    # ── sweep: instances of classes that were fixed in one place only ──────

    def test_no_module_left_with_a_shared_temp_name(self):
        """The unique-temp fix has to hold for every writer, not the last one found."""
        import inspect
        from hermes_dashboard import build, gen_config, history, settings as st
        from hermes_dashboard.collectors import anthropic_cost, codex_quota
        for mod in (build, st, history, gen_config, anthropic_cost, codex_quota):
            src = inspect.getsource(mod)
            for pattern in ('with_suffix(".tmp")', 'cache_path() + ".tmp"'):
                self.assertNotIn(pattern, src,
                                 f"{mod.__name__} derives its temp name from the target")

    def test_every_sql_literal_from_config_is_quoted(self):
        """paid()/free_cond() quote; the primary card used to build its own."""
        import inspect
        from hermes_dashboard import gen_usage
        cfg = config.Config({"providers": {"primary": {"id": "o'brien"}}})
        config.set_current(cfg)
        self.assertEqual(common.sql_str(common.primary_id()), "'o''brien'")
        self.assertNotIn("billing_provider='{pid}'", inspect.getsource(gen_usage))

    def test_timezone_survives_a_hand_edited_config(self):
        """validate() guards the form; the build reads a file people edit by hand."""
        for bad in (24, -24, "x", None, 99999):
            config.set_current(config.Config({"timezone": {"offset_hours": bad}}))
            self.assertEqual(common.tz().utcoffset(None).total_seconds(), 0,
                             f"offset_hours={bad!r} must fall back to UTC, not crash")
        config.set_current(config.Config({"timezone": {"offset_hours": 7}}))
        self.assertEqual(common.tz().utcoffset(None).total_seconds(), 7 * 3600)

    def test_per_job_telemetry_ignores_sessions_with_no_model_call(self):
        import inspect
        from hermes_dashboard import gen_cron
        src = inspect.getsource(gen_cron)
        self.assertIn("coalesce(api_call_count,0) > 0", src,
                      "the per-job token table must apply the same activity rule")
        self.assertNotIn('coalesce(api_call_count,0)>0 "', src,
                         "the activity predicate must come from active(), not a hand-typed copy")

    def test_language_codes_are_safe_as_filenames_and_js_keys(self):
        from hermes_dashboard import render
        # the old check was length-only, so these passed
        for bad in ('a"b', "../x", "a/b", "e n"):
            errs = config.Config({"i18n": {"languages": [bad]}}).validate()
            self.assertTrue(any("i18n.languages" in e for e in errs), f"{bad!r} must be rejected")
        # a language the engine can actually translate
        self.assertEqual(config.Config({"i18n": {"default": "en",
                                                 "languages": ["en", "ru"]}}).validate(), [])
        # ...and one it cannot: the build would write index.pt-BR.html full of English
        self.assertTrue(any("locales/pt-BR.json" in e for e in
                            config.Config({"i18n": {"default": "en",
                                                    "languages": ["en", "pt-BR"]}}).validate()),
                        "a listed language with no locale file must be reported")
        # and the redirect map is escaped regardless
        cfg = config.Config({"i18n": {"default": "en", "languages": ["en", 'a"b']}})
        config.set_current(cfg)
        head = render.head(cfg, "t", "en")
        js = head[head.find("pg={"):head.find("};if(hl")]
        self.assertNotIn('"a"b"', js, "an unescaped key breaks the whole script block")
        self.assertIn('a\\"b', js)

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

    def test_a_bad_byte_in_an_agent_file_does_not_stop_the_build(self):
        """UnicodeDecodeError is a ValueError, so `except OSError` let it through.

        A killed writer leaves a truncated multibyte sequence in exactly the
        files the dashboard reads. Before this, one such byte in MEMORY.md
        produced zero pages.
        """
        home = make_home()
        (home / "memories" / "MEMORY.md").write_bytes(b"rule\n\xd0\xa1\xd1 truncated\n")
        pages = self._build(home)
        self.assertIn("index.html", pages)
        self.assertTrue(len(pages["index.html"]) > 1000)

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
