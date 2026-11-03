"""Contract check against a real Hermes core: the core's own code writes the home, the dashboard reads it.

tests/ builds state.db, cron/jobs.json and config.yaml by hand, and every reader here fails open (a missing
table or column turns a number into "—" or a card into nothing), so a format change in Hermes would leave the
unit tests green and the pages quietly wrong. Here the core writes a home the way it does in production:
`hermes config set` writes config.yaml, `hermes cron create / run` writes jobs.json and runs one LLM job, one
script job and one failing script job, and `hermes -z` runs a one-shot turn that calls the memory tool. The
model is a stub OpenAI-compatible endpoint on 127.0.0.1 with fixed usage, so there is no network and no key,
and every number the dashboard shows is known in advance.

Needs Hermes core importable by this interpreter (`python -m hermes_cli.main`); CI installs the latest core
release (the `core-contract` job). Not part of `python -m unittest discover -s tests`: run
`python -m unittest discover -s contract -v`.

memory_store.db is not covered: the holographic memory plugin writes it, not the core.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hermes_dashboard import common, config, gen_cron, gen_usage, i18n  # noqa: E402
from hermes_dashboard import build as dashboard_build  # noqa: E402

# Per model call: 1200 prompt tokens of which 1000 came from the prompt cache, 30 completion tokens.
PROMPT, CACHED, COMPLETION = 1200, 1000, 30
FRESH = PROMPT - CACHED
FAIL_ERROR = "disk check failed"
MEMORY_TEXT = "The user prefers tea."


class StubModel(BaseHTTPRequestHandler):
    """Answers every chat completion with the same text and usage. A user message with "remember" gets one
    `memory` tool call first, so the core runs a real tool and records it."""

    calls: list = []

    def log_message(self, *args):
        pass

    def _json(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            return self._json(200, {"object": "list", "data": [{"id": "stub-model", "object": "model"}]})
        self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if not self.path.endswith("/chat/completions"):
            return self._json(404, {"error": {"message": "not found"}})
        StubModel.calls.append({"stream": bool(request.get("stream"))})
        messages = request.get("messages", [])
        tools = {tool.get("function", {}).get("name") for tool in request.get("tools") or []}
        remember = any("remember" in str(m.get("content")) for m in messages if m.get("role") == "user")
        call = None
        if remember and "memory" in tools and not any(m.get("role") == "tool" for m in messages):
            call = {"index": 0, "id": "call_contract_1", "type": "function", "function": {
                "name": "memory", "arguments": json.dumps({"action": "add", "target": "memory",
                                                           "content": MEMORY_TEXT})}}
        usage = {"prompt_tokens": PROMPT, "completion_tokens": COMPLETION, "total_tokens": PROMPT + COMPLETION,
                 "prompt_tokens_details": {"cached_tokens": CACHED}}
        base = {"id": "chatcmpl-contract", "created": int(time.time()), "model": "stub-model"}
        text = "All quiet: nothing to report."
        if not request.get("stream"):
            message = {"role": "assistant", "content": None if call else text}
            if call:
                message["tool_calls"] = [{k: v for k, v in call.items() if k != "index"}]
            return self._json(200, dict(base, object="chat.completion", usage=usage, choices=[
                {"index": 0, "message": message, "finish_reason": "tool_calls" if call else "stop"}]))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        delta = {"role": "assistant", "tool_calls": [call]} if call else {"role": "assistant", "content": text}
        for chunk in (dict(base, choices=[{"index": 0, "delta": delta, "finish_reason": None}]),
                      dict(base, choices=[{"index": 0, "delta": {},
                                           "finish_reason": "tool_calls" if call else "stop"}]),
                      dict(base, choices=[], usage=usage)):
            self.wfile.write(f"data: {json.dumps(dict(chunk, object='chat.completion.chunk'))}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


def environment(home):
    env = dict(os.environ, HERMES_HOME=str(home), PYTHONIOENCODING="utf-8", NO_COLOR="1")
    env.pop("HERMES_SESSION_KEY", None)
    return env


def core(home, *args, ok=(0,)):
    """`hermes <args>` through this interpreter, so the core under test is the one importable here."""
    result = subprocess.run([sys.executable, "-m", "hermes_cli.main", *args], env=environment(home),
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    if result.returncode not in ok:
        raise AssertionError(f"hermes {' '.join(args)} failed ({result.returncode}):\n"
                             f"{result.stdout}\n{result.stderr}")
    return result.stdout


def gateway_pid(home):
    """PID of the gateway Hermes says belongs to `home` (its own identity check), or None."""
    result = subprocess.run(
        [sys.executable, "-c", "import sys; from pathlib import Path; from gateway.status import "
         "live_gateway_pid_for_home as live; print(live(Path(sys.argv[1])) or '')", str(home)],
        env=environment(home), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    if result.returncode != 0:
        raise AssertionError(f"cannot ask Hermes for the gateway of {home}:\n{result.stderr}")
    return int(result.stdout.strip()) if result.stdout.strip() else None


def create(home, name, *args):
    output = core(home, "cron", "create", "every 1h", *args, "--name", name, "--deliver", "local")
    match = re.search(r"Created job: (\S+)", output)
    if not match:
        raise AssertionError(f"no job id in `hermes cron create` output:\n{output}")
    return match.group(1)


DASHBOARD = {
    "i18n": {"default": "en", "languages": ["en"]},
    "views": {"config_map": False},
    "providers": {"primary": {"id": "custom", "label": "Stub"}, "paid": [], "free": [],
                  "fallback_sources": ["telegram", "cron"]},
}


class CoreWrittenHome(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        StubModel.calls = []
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), StubModel)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.home = home = Path(tempfile.mkdtemp(prefix="dashboard-contract-"))
        (home / "scripts").mkdir()
        (home / "scripts" / "hello.py").write_text('print("all good")\n', encoding="utf-8")
        (home / "scripts" / "fail.py").write_text(
            f'import sys\nprint("{FAIL_ERROR}", file=sys.stderr)\nsys.exit(3)\n', encoding="utf-8")
        for key, value in (("model.provider", "custom"), ("model.default", "stub-model"),
                           ("model.base_url", f"http://127.0.0.1:{cls.server.server_port}/v1"),
                           ("memory.memory_char_limit", "3000"), ("memory.user_char_limit", "1500")):
            core(home, "config", "set", key, value)
        cls.llm_job = create(home, "morning brief", "Check the inbox and report")
        cls.script_job = create(home, "watchdog", "--no-agent", "--script", "hello.py")
        cls.broken_job = create(home, "broken watchdog", "--no-agent", "--script", "fail.py")
        core(home, "cron", "run", cls.llm_job)
        cron_calls = sum(call["stream"] for call in StubModel.calls)
        core(home, "cron", "run", cls.script_job)
        core(home, "cron", "run", cls.broken_job, ok=(0, 1))  # 1: the run failed, as it should
        core(home, "-z", "Please remember this")
        # Calls of the agent loop; a non-streamed call is an auxiliary one (the session title).
        cls.cron_calls = cron_calls
        cls.agent_calls = sum(call["stream"] for call in StubModel.calls)
        os.environ["HERMES_HOME"] = str(home)
        cls.cfg = config.Config(json.loads(json.dumps(DASHBOARD)))
        config.set_current(cls.cfg)
        i18n.set_lang("en")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        # The LLM turns started a gateway for this home in the background (`gateway run --idle-exit`). Its
        # cron jobs count as work, so it never idles out: left alone it outlives the test, and the deleted
        # home, for good. Stop it the way an operator would, scoped to this home only.
        try:
            leftover = gateway_pid(cls.home)
            if leftover is not None:
                core(cls.home, "gateway", "stop", "--yes")
                deadline = time.monotonic() + 60
                while leftover is not None and time.monotonic() < deadline:
                    time.sleep(0.5)
                    leftover = gateway_pid(cls.home)
        finally:
            shutil.rmtree(cls.home, ignore_errors=True)
        if leftover is not None:
            raise AssertionError(f"the gateway of the test home is still running (pid {leftover})")

    def test_the_core_wrote_what_the_scenario_needs(self):
        # Guards the fixture itself: without these the checks below would prove nothing.
        self.assertGreaterEqual(self.cron_calls, 1)
        self.assertGreater(self.agent_calls, self.cron_calls + 1)  # the one-shot turn: tool call + answer
        self.assertTrue((self.home / "state.db").is_file())

    def test_config_yaml_values(self):
        self.assertEqual(common.yaml_get("model.provider", ""), "custom")
        self.assertEqual(common.yaml_get("model.default", ""), "stub-model")
        memory = dashboard_build.memory_facts(self.cfg)
        self.assertEqual((memory["MEMLIM"], memory["USERLIM"]), (3000, 1500))
        stored = (self.home / "memories" / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn(MEMORY_TEXT, stored)
        self.assertEqual(memory["MEMCH"], len(stored))

    def test_times_are_epoch_seconds(self):
        # Every window ("7 days", "30 days", the daily chart) compares these with strftime('%s', …): a text
        # timestamp would pass every window, so a week would silently count the whole history.
        import sqlite3
        now = time.time()
        with sqlite3.connect(self.home / "state.db") as db:
            for table, column in (("sessions", "started_at"), ("messages", "timestamp")):
                values = db.execute(f"SELECT typeof({column}), {column} FROM {table}").fetchall()
                self.assertTrue(values, table)
                for kind, value in values:
                    self.assertEqual(kind, "real", f"{table}.{column} = {value!r}")
                    self.assertLess(abs(now - value), 3600, f"{table}.{column} = {value!r}")

    def test_overview_numbers(self):
        k = dashboard_build.collect_kpis(self.cfg)
        expected = {
            "SESS7": 2, "SESS": 2, "PASSIVE7": 0,       # the cron LLM run and the one-shot turn
            "CRON7": 1,
            "FALLBACK7": 0, "PAIDCLI7": 0,
            "TOK7_CALLS": self.agent_calls,
            "TOK7_IN": FRESH * self.agent_calls,         # input means fresh tokens, the cache is apart
            "TOK7_OUT": COMPLETION * self.agent_calls,
            "TOK7_REASON": 0,
            "TOK7_CRON": FRESH * self.cron_calls,
            "MEMCALLS": 1,
        }
        self.assertEqual({key: k[key] for key in expected}, expected)
        self.assertGreaterEqual(k["MSGS"], 6)  # user + answer each, plus the tool call and its result

    def test_cron_cards(self):
        self.assertEqual(gen_cron.cron_db_stats(), (1, FRESH * self.cron_calls))
        card = gen_cron.efficiency_card()
        for label, value in (("Jobs enabled", 3), ("Script-only · no LLM", 2), ("LLM jobs · with a model", 1),
                             ("Failing now", 1), ("LLM cron sessions · 7d", 1)):
            self.assertRegex(card, rf'<div class="uml">{re.escape(label)}</div><div class="umv">{value}</div>')
        self.assertRegex(card, rf"<b>broken watchdog</b> — error · [^<]*{FAIL_ERROR}")

    def test_job_telemetry_names_the_llm_job(self):
        card = gen_cron.job_telemetry_card()
        self.assertIn("morning brief", card, "the LLM cron run is not attributed to its job")
        self.assertNotIn("deleted job", card)
        row = re.search(r"<tr><td>morning brief</td><td>(\d+)</td>", card)
        self.assertIsNotNone(row, card)
        self.assertEqual(row.group(1), "1")

    def test_cost_view(self):
        usage, analytics = gen_usage.build()
        self.assertNotEqual(usage, "")
        self.assertIn(common.fmt_tok(FRESH * self.agent_calls), usage)
        self.assertIn("memory", analytics)  # top tools

    def test_full_build_has_no_failed_section(self):
        cfgp = self.home / "dashboard" / "dashboard.json"
        cfgp.parent.mkdir(exist_ok=True)
        cfgp.write_text(json.dumps(DASHBOARD), encoding="utf-8")
        out = self.home / "public"
        result = subprocess.run(
            [sys.executable, "-m", "hermes_dashboard.build", "--config", str(cfgp), "--out", str(out)],
            env=dict(environment(self.home), PYTHONPATH=str(ROOT)), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr)
        failed = [line for line in result.stderr.splitlines() if line.startswith(("[section]", "[config]"))]
        self.assertEqual(failed, [])
        page = (out / "index.html").read_text(encoding="utf-8")
        self.assertIn("LLM job telemetry · who spends tokens", page)
        self.assertNotIn("Usage data unavailable (state.db).", page)


if __name__ == "__main__":
    unittest.main()
