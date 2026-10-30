"""The memory map tab (gen_memory_map.py) on a fake exporter — no plugin, no agent needed.

Requirements, in the owner's words, each a test below:
- a dashboard that does not turn the tab on is the dashboard it was: no tab, no link, nothing spawned;
- turned on, the tab sits in the sidebar and the phone tabs like the others, the Memory view links to it, and it
  speaks both languages;
- a broken export (crash, garbage, wrong format, hang, missing plugin or store) costs only this tab: it says it is
  unavailable instead of showing numbers, the log gets a "[section] memory-map failed" line, the build goes on;
- memory text is written from chat messages, so markup in it is shown as text, never run;
- the tab shows what the export decided (trust words, what is weak, the health category) without re-deciding;
- the exporter is asked for the configured store, in the dashboard's zone;
- the tab says it is experimental, in both languages.

The snapshots below are shaped like the plugin's real output (schema_version 1, docs/memory-map.md there),
including its dirty cases: an old record with no source, a corrected version with no text, a hidden version."""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hermes_dashboard import build, config, i18n, render  # noqa: E402

XSS = '<script>alert("mm")</script>'
IMG = '<img src=x onerror=alert(1)>'


def snapshot(**over) -> dict:
    """A small family store as the exporter prints it."""
    fact = {"id": 1, "text": "Varya is in class 2B.", "ents": [3], "cat": "school", "origin": "said", "trust": 0.6,
            "trust_level": "normal", "status": "current", "from": None, "to": None, "to_exact": None,
            "rec": "2026-10-20", "first": "2026-09-01", "via": "tool", "chat": "dm", "by": "Dmitry",
            "conf": [{"d": "2026-10-21", "by": "Alena"}], "recalls": 4, "helpful": 1, "last": None,
            "versions": [{"seq": 1, "status": "corrected", "text": None, "from": None, "to": None, "to_exact": True,
                          "rec": "2026-09-01", "closed": "2026-09-02", "origin": "said", "via": "tool"},
                         {"seq": 2, "status": "changed", "text": None, "hidden": True, "from": None, "to": None,
                          "to_exact": True, "rec": "2026-09-02", "closed": "2026-10-20", "origin": None, "via": None},
                         {"seq": 3, "status": "current", "text": "Varya is in class 2B.", "from": None, "to": None,
                          "to_exact": True, "rec": "2026-10-20", "closed": None, "origin": "said", "via": "tool"}],
            "mergedFrom": [], "weak": []}
    legacy = dict(fact, id=2, text="Varya is allergic to peanuts.", cat="health", origin="unknown", trust=0.3,
                  trust_level="normal", via="legacy", chat=None, by=None, conf=[], versions=[])
    data = {"schema_version": 1,
            "SNAP": {"made": "2026-10-28T03:14:00+07:00", "tz": "+07:00", "store": "memory_store.db",
                     "schema": {"store": "2"}, "counts": {"current": 2, "legacy": 1, "entities": 1},
                     "week_from": "2026-10-21", "excluded": [], "experience": {"included": False},
                     "dreaming": {"read": False, "reason": "not_requested", "files": {}}},
            "ENT": [{"id": 3, "name": "Varya", "type": None, "declared": True, "aliases": [], "text_aliases": [],
                     "standing": "established", "stale": False, "since": "2026-09-01", "facts": 2}],
            "FACTS": [fact, legacy], "CONFLICTS": [], "PENDING": [], "TOMBS": [{"id": 9, "d": "2026-10-25"}],
            "MERGES": [], "HIDDEN": [], "FEED": [{"d": "2026-10-27", "k": "new", "f": 1}], "ATTEMPTS": []}
    data.update(over)
    return data


def fake_plugin(home: Path, code: str) -> str:
    """A plugin folder whose memory_map.py runs *code* (sys and json imported)."""
    plug = home / "plug"
    plug.mkdir(parents=True, exist_ok=True)
    (plug / "memory_map.py").write_text("import json, sys, time\n" + code + "\n", encoding="utf-8")
    return "plug"


def printing(data) -> str:
    return "sys.stdout.write(%r)" % json.dumps(data)


def panel(html: str, key: str) -> str:
    """One page of the tab: from its <div class="mm-pg" data-pg="key"> to the next page."""
    m = re.search(r'<div class="mm-pg[^"]*" data-pg="%s">' % re.escape(key), html)
    assert m, "no panel " + key
    end = html.find('<div class="mm-pg', m.end())
    return html[m.end():end if end > 0 else len(html)]


def new_home() -> Path:
    home = Path(tempfile.mkdtemp(prefix="hd-mm-"))
    _HOMES.append(home)
    (home / "memory_store.db").write_bytes(b"")   # the exporter is fake; only its presence is checked
    return home


def cfg_for(home: Path, extra: dict | None = None) -> config.Config:
    data = {"views": {"memory_map": True}, "memory_map": {"plugin_dir": "plug", "timeout_s": 20},
            "timezone": {"offset_hours": 7}}
    for k, v in (extra or {}).items():
        data[k] = dict(data.get(k, {}), **v) if isinstance(v, dict) else v
    cfg = config.Config(data)
    cfg.home = home
    return cfg


def build_pages(home: Path, data: dict) -> tuple[dict[str, str], str]:
    cfgp = home / "dashboard" / "dashboard.json"
    cfgp.parent.mkdir(exist_ok=True)
    data = dict({"i18n": {"default": "en", "languages": ["en", "ru"]}, "views": {"config_map": False}}, **data)
    cfgp.write_text(json.dumps(data), encoding="utf-8")
    out = home / "public"
    # the build's stderr carries the exporter's, in any encoding: read it as UTF-8 with replacements, as a log would
    env = dict(os.environ, HERMES_HOME=str(home), PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")
    r = subprocess.run([sys.executable, "-m", "hermes_dashboard.build", "--config", str(cfgp), "--out", str(out)],
                       env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    if r.returncode != 0:
        raise AssertionError("build failed: " + r.stderr[-2000:])
    return {p.name: p.read_text(encoding="utf-8") for p in out.glob("*.html")}, r.stderr


_HOMES: list[Path] = []


def tearDownModule():
    for home in _HOMES:   # every build leaves a home and its public/ behind otherwise
        shutil.rmtree(home, ignore_errors=True)


class MemoryMapTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_lang("en")

    def test_the_exporter_gets_no_credentials(self):
        """The exporter is the plugin's code, at a path the settings page can change: it gets the agent home and none
        of the dashboard's own credentials (the Anthropic admin key the usage collector reads)."""
        home = new_home()
        seen = home / "env.json"
        names = ("ANTHROPIC_ADMIN_KEY", "HERMES_DASHBOARD_ANTHROPIC_ADMIN_KEY", "HERMES_HOME")
        fake_plugin(home, "import os\nopen(%r, 'w').write(json.dumps({k: os.environ.get(k) for k in %r}))\n"
                    % (str(seen), names) + printing(snapshot()))
        with mock.patch.dict(os.environ, {"ANTHROPIC_ADMIN_KEY": "sk-ant-admin01-test",
                                          "HERMES_DASHBOARD_ANTHROPIC_ADMIN_KEY": "sk-ant-admin01-test2"}):
            data, why = build.gen_memory_map.load(cfg_for(home))
        self.assertIsNone(why)
        got = json.loads(seen.read_text(encoding="utf-8"))
        self.assertIsNone(got["ANTHROPIC_ADMIN_KEY"])
        self.assertIsNone(got["HERMES_DASHBOARD_ANTHROPIC_ADMIN_KEY"])
        self.assertEqual(got["HERMES_HOME"], str(home))

    def test_stored_text_never_reaches_the_build_log(self):
        """The build log is not behind the password. A record the view cannot draw, or an exporter that dies with
        memory text in its error, leaves a failure line without that text, and the rest of the dashboard is built."""
        secret = "Пароль от почты: hunter2"
        cases = {
            "a contradiction's other side is text": printing(snapshot(CONFLICTS=[{"a": 1, "b": secret}])),
            "a pending contradiction names text": printing(snapshot(
                PENDING=[{"kind": "conflict", "n": 1, "a": 1, "b": secret}])),
            "the exporter dies on a stored text": "raise ValueError(%r)" % secret,
        }
        for label, code in cases.items():
            with self.subTest(label):
                home = new_home()
                fake_plugin(home, code)
                pages, err = build_pages(home, {"views": {"config_map": False, "memory_map": True},
                                                "memory_map": {"plugin_dir": "plug"}})
                self.assertNotIn("hunter2", err)
                self.assertIn('id="v-cost"', pages["index.html"])
                self.assertIn('id="v-mmap"', pages["index.html"])

    @unittest.skipUnless(os.name == "posix", "a process group is POSIX; on Windows only the exporter itself is killed")
    def test_a_timed_out_export_leaves_no_process_behind(self):
        """memory_map.python may be a wrapper (uv run, a shell script) that starts the real exporter as its child:
        a timeout stops both, so a hanging export does not pile up a process every 30 minutes."""
        home = new_home()
        alive = home / "alive"
        child = "import time, pathlib; time.sleep(3); pathlib.Path(%r).write_text('x')" % str(alive)
        fake_plugin(home, "import subprocess\nsubprocess.Popen([sys.executable, '-c', %r])\ntime.sleep(30)" % child)
        buf = io.StringIO()
        with redirect_stderr(buf):
            data, why = build.gen_memory_map.load(cfg_for(home, {"memory_map": {"timeout_s": 1}}))
        self.assertEqual(why.code, "timeout")
        time.sleep(4)
        self.assertFalse(alive.exists(), "the exporter's child outlived the timeout")

    @unittest.skipUnless(os.name == "posix", "file modes are POSIX")
    def test_the_build_says_when_every_local_user_can_read_the_map(self):
        """The tab puts memory text into the pages. When every local account can reach them (each folder up to the
        output enterable by others), the build log says so; a folder closed to others ends the warning."""
        home = new_home()
        fake_plugin(home, printing(snapshot()))
        home.chmod(0o755)
        _, err = build_pages(home, {"views": {"config_map": False, "memory_map": True},
                                    "memory_map": {"plugin_dir": "plug"}})
        if all(p.stat().st_mode & 0o001 for p in [home, *home.parents]):
            self.assertIn("every local user", err)
        home.chmod(0o750)
        _, err = build_pages(home, {"views": {"config_map": False, "memory_map": True},
                                    "memory_map": {"plugin_dir": "plug"}})
        self.assertNotIn("every local user", err)

    def test_off_changes_nothing_and_runs_nothing(self):
        """Off (the default, or said so) — the same sidebar, tabs and Memory view, and the exporter never runs,
        even when a plugin folder is configured."""
        home = new_home()
        marker = home / "ran"
        fake_plugin(home, "open(%r, 'w').write('x')\n" % str(marker) + printing(snapshot()))
        plain = config.Config({})
        off = config.Config({"views": {"memory_map": False}, "memory_map": {"plugin_dir": "plug"}})
        self.assertFalse(plain.get("views.memory_map"), "the tab is off unless asked for")
        for cfg in (plain, off):
            self.assertEqual(render.view_list(cfg), render.view_list(config.Config({"views": {"config_map": True}})))
            self.assertNotIn("mmap", render.rail(cfg, "en", "over", "x", "y", "z", "s", "c"))
            self.assertNotIn("mmap", render.bottom_tabs(cfg, "en", "over"))
        host = {"sync_at": "03:00"}
        k = {"FACTS": 3, "MSGS": 10, "MEMCALLS": 1}
        self.assertEqual(build.view_memory(plain, k, build.MEMORY_UNKNOWN, {}, host),
                         build.view_memory(off, k, build.MEMORY_UNKNOWN, {}, host))
        self.assertNotIn("#mmap", build.view_memory(off, k, build.MEMORY_UNKNOWN, {}, host))
        pages, _err = build_pages(home, {"memory_map": {"plugin_dir": "plug"}})
        self.assertFalse(marker.exists(), "the exporter ran although the tab is off")
        for name, html in pages.items():
            # "h1 .exp": the tab's own CSS, even a rule shared pages could carry, stays out (Codex review: the
            # experimental-label rule sat in style.css, so every page changed with the tab off)
            for needle in ("v-mmap", "mmap", "mm-pg", "Memory map", "Карта памяти", "h1 .exp"):
                self.assertNotIn(needle, html, f"{needle!r} in {name} with the tab off")

    def test_on_the_tab_is_in_both_navigations_and_linked_from_memory(self):
        home = new_home()
        fake_plugin(home, printing(snapshot()))
        pages, err = build_pages(home, {"views": {"config_map": False, "memory_map": True},
                                        "memory_map": {"plugin_dir": "plug"}})
        en, ru = pages["index.html"], pages["index.ru.html"]
        self.assertNotIn("memory-map failed", err)
        self.assertEqual(en.count('<button data-v="mmap">'), 2, "one button in the sidebar, one in the phone tabs")
        self.assertIn('id="v-mmap"', en)
        mem = en[en.index('id="v-mem"'):en.index('id="v-mmap"')]
        self.assertIn('href="#mmap"', mem, "the Memory view links to the map")
        self.assertIn(">Memory map<", en)
        self.assertIn(">Карта памяти<", ru)
        self.assertIn("Ждут твоего решения", ru)
        self.assertIn("Varya is in class 2B.", en)
        for name, html in pages.items():
            self.assertEqual(html.count("<div"), html.count("</div>"), f"div balance in {name}")
            self.assertEqual(html.count("<section"), html.count("</section>"), f"section balance in {name}")

    def test_the_tab_says_it_is_experimental_in_both_languages(self):
        """The map is a new, experimental feature (the plugin's docs/memory-map.md): its header says so in the
        reader's language, next to the title, whether the export worked or not."""
        for lang, word in (("en", "experimental"), ("ru", "экспериментально")):
            with self.subTest(lang):
                i18n.set_lang(lang)
                for html in (build.gen_memory_map.view(cfg_for(new_home()), snapshot()),
                             build.gen_memory_map.view(cfg_for(new_home()), None,
                                                       build.gen_memory_map.MapUnavailable("exit", "rc=1", 1))):
                    h1 = re.search(r"<h1>(.*?)</h1>", html[html.index('id="v-mmap"'):]).group(1)
                    self.assertIn('class="exp"', h1)
                    self.assertIn(f">{word}<", h1)
                    self.assertIn(".vh h1 .exp{", html, "the label is drawn as a label, not as plain text")
        i18n.set_lang("en")

    def test_night_records_the_export_left_out_are_said_not_hidden(self):
        """The export leaves out a record of the night's files it cannot read and lists it (SNAP.dreaming.invalid):
        the person sees that something was left out, without its contents."""
        snap = snapshot()
        snap["SNAP"]["dreaming"] = {"read": True, "reason": None, "files": {"proposals": "ok", "report": "ok",
                                                                            "queue": "absent"},
                                    "invalid": [{"file": "proposals", "id": "D-5", "why": "keep"},
                                                {"file": "report", "id": "md_decays[2]", "why": "record"}]}
        about = panel(build.gen_memory_map.view(cfg_for(new_home()), snap), "about")
        self.assertIn("2 records of another shape were left out", about)
        clean = panel(build.gen_memory_map.view(cfg_for(new_home()), snapshot()), "about")
        self.assertNotIn("of another shape", clean)

    def test_a_broken_export_costs_only_this_tab(self):
        bad = {
            "crash": "sys.stderr.write('Traceback: boom'); sys.exit(3)",
            "garbage": "sys.stdout.write('not json at all')",
            "empty output": "pass",
            "empty object": "sys.stdout.write('{}')",
            "unknown format": printing(snapshot(schema_version=99)),
            "wrong shape": printing(snapshot(FACTS={"1": "x"})),
            "a list": "sys.stdout.write('[1, 2]')",
            "half a record": "sys.stdout.write(%r)" % json.dumps(snapshot())[:400],
            "bad bytes": "sys.stdout.buffer.write(b'\\xff\\xfe{')",
            "hangs": "time.sleep(10)",
        }
        for label, code in bad.items():
            with self.subTest(label):
                home = new_home()
                fake_plugin(home, code)
                cfg = cfg_for(home, {"memory_map": {"timeout_s": 2}})
                buf = io.StringIO()
                with redirect_stderr(buf):
                    data, why = build.gen_memory_map.load(cfg)
                self.assertIsNone(data)
                self.assertIsNotNone(why)
                self.assertIn("[section] memory-map failed", buf.getvalue())
                html = build.gen_memory_map.view(cfg, data, why)
                self.assertIn("Memory map unavailable", html)
                self.assertNotIn("mm-kpi", html, "no numbers on a failed export")
        for label, cfg_extra in {"no plugin folder set": {"memory_map": {"plugin_dir": ""}},
                                 "no memory_map.py": {"memory_map": {"plugin_dir": "nowhere"}},
                                 "no store": {"memory_map": {"store": "missing.db"}}}.items():
            with self.subTest(label):
                home = new_home()
                fake_plugin(home, printing(snapshot()))
                buf = io.StringIO()
                with redirect_stderr(buf):
                    data, why = build.gen_memory_map.load(cfg_for(home, cfg_extra))
                self.assertIsNone(data)
                self.assertIn("[section] memory-map failed", buf.getvalue())
        # and in a real build: the page is written, the other views are there, the tab says why
        home = new_home()
        fake_plugin(home, "sys.exit(1)")
        pages, err = build_pages(home, {"views": {"config_map": False, "memory_map": True},
                                        "memory_map": {"plugin_dir": "plug"}})
        en = pages["index.html"]
        self.assertIn("[section] memory-map failed", err)
        self.assertIn('id="v-cost"', en)
        self.assertIn("Memory map unavailable", en)
        self.assertIn("exit code 1", en)

    def test_memory_text_is_shown_as_text(self):
        """Every string of the snapshot came from a chat: markup in it must not reach the page as markup."""
        s = snapshot()
        f = s["FACTS"][0]
        f.update(text=XSS, cat=IMG, by=XSS, conf=[{"d": "2026-10-21", "by": IMG}])
        f["versions"][2]["text"] = XSS
        f["mergedFrom"] = [{"id": 7, "text": IMG, "d": "2026-10-23", "prop": XSS}]
        s["ENT"][0].update(name=XSS, aliases=[IMG], text_aliases=[XSS])
        s["MERGES"] = [{"from": 7, "into": 1, "d": "2026-10-23", "prop": "D-1", "text": XSS}]
        s["FACTS"].append(dict(f, id=5, status="ended", to="2026-10-25"))
        s["CONFLICTS"] = [{"id": "C1", "a": 1, "b": 5, "score": 0.9, "shared": [3]}]
        s["PENDING"] = [{"kind": "merge", "id": XSS, "keep": 1, "copy": 5, "covered": False, "extra": [IMG],
                         "created": "2026-10-27", "asked": None, "status": "open", "current": True},
                        {"kind": "memory_conflict", "id": "M-1", "fact": 1, "entry": XSS, "fact_numbers": [IMG],
                         "asked": "2026-10-27", "status": "open"},
                        {"kind": "relevant", "id": "R-1", "text": IMG, "last_mention_days": 61,
                         "asked": "2026-10-27", "status": "open"}]
        s["HIDDEN"] = [{"id": 8, "d": "2026-10-01", "why": XSS, "status": "current"}]
        s["FEED"] = [{"d": "2026-10-27", "k": "conf", "f": 1, "by": XSS},
                     {"d": "2026-10-27", "k": "night", "at": "2026-10-27T03:05:00+07:00", "alerts": [XSS],
                      "stats": {XSS: 1}}]
        s["SNAP"].update(store=XSS, tz=IMG)
        home = new_home()
        for lang in ("en", "ru"):
            i18n.set_lang(lang)
            html = build.gen_memory_map.view(cfg_for(home), s)
            self.assertNotIn("<script>alert", html)
            self.assertNotIn("<img src=x", html)
            self.assertIn("&lt;script&gt;alert(&quot;mm&quot;)&lt;/script&gt;", html)
            self.assertIn("&lt;img src=x onerror=alert(1)&gt;", html)

    def test_shows_what_the_export_decided(self):
        """Trust words, weak basis and categories come from the export as they are: the legacy record at 0.3 that
        the export calls normal and not weak stays so, and the health category is shown."""
        html = build.gen_memory_map.view(cfg_for(new_home()), snapshot())
        legacy = panel(html, "f2")
        self.assertIn("normal trust", legacy)
        self.assertNotIn("weak trust", legacy, "the 0.3 the export calls normal is not re-judged by the number")
        self.assertNotIn("On a weak basis", legacy)
        weak = panel(html, "weak")
        self.assertIn("No facts on a weak basis.", weak)
        self.assertIn("1 old record without a source is not counted as weak", weak)
        self.assertIn(">health<", html)
        self.assertIn("Varya is allergic to peanuts.", html)
        # a corrected version and a hidden one show no text; the current one does
        fact = panel(html, "f1")
        self.assertIn("The text of a corrected mistake is not shown", fact)
        self.assertIn("Hidden by the safety screen.", fact)
        # forgotten: the number and the date, nothing else
        removed = panel(html, "removed")
        self.assertIn("№9", removed)

    def test_the_exporter_gets_the_configured_store_and_zone(self):
        home = new_home()
        seen = home / "argv.json"
        fake_plugin(home, "json.dump(sys.argv[1:], open(%r, 'w'))\n" % str(seen) + printing(snapshot()))
        data, why = build.gen_memory_map.load(cfg_for(home))
        self.assertIsNone(why)
        self.assertEqual(data["SNAP"]["store"], "memory_store.db")
        argv = json.loads(seen.read_text())
        self.assertEqual(argv[argv.index("--store") + 1], str(home / "memory_store.db"))
        self.assertEqual(argv[argv.index("--tz") + 1], "+07:00")
        self.assertIn("--json", argv)
        self.assertEqual(argv[argv.index("--hermes-home") + 1], str(home))
        build.gen_memory_map.load(cfg_for(home, {"memory_map": {"dreaming": False},
                                                 "timezone": {"offset_hours": -3}}))
        argv = json.loads(seen.read_text())
        self.assertNotIn("--hermes-home", argv, "the nightly review is not read when it is turned off")
        self.assertEqual(argv[argv.index("--tz") + 1], "-03:00")


    def test_the_exporter_is_given_the_agents_search_settings(self):
        """Which side of a contradiction the agent leans on depends on the plugin's search settings (trust floor,
        decay): the exporter gets the agent's config.yaml when there is one, and nothing when there is none (the
        plugin then runs on its defaults, and so does the export)."""
        home = new_home()
        seen = home / "argv.json"
        fake_plugin(home, "json.dump(sys.argv[1:], open(%r, 'w'))\n" % str(seen) + printing(snapshot()))
        build.gen_memory_map.load(cfg_for(home))
        self.assertNotIn("--config", json.loads(seen.read_text()))
        (home / "config.yaml").write_text("plugins: {}\n", encoding="utf-8")
        build.gen_memory_map.load(cfg_for(home))
        argv = json.loads(seen.read_text())
        self.assertEqual(argv[argv.index("--config") + 1], str(home / "config.yaml"))


def rich_snapshot() -> dict:
    """Every page has something: weak facts, names in question, a week of events, everything removed four ways,
    the night's questions, contradictions with and without an answer about the order."""
    s = snapshot()
    base = s["FACTS"][0]

    def fact(fid, text, **kw):
        return dict(base, **dict({"id": fid, "text": text, "versions": [], "conf": []}, **kw))

    s["FACTS"] = [
        fact(1, "Varya swims on Tuesdays.", rec="2026-09-10", weak=[], trust=0.7, trust_level="strong"),
        fact(2, "Varya swims on Wednesdays.", rec="2026-10-25", weak=["low_trust"], trust=0.25,
             trust_level="weak"),
        fact(3, "Alena does not eat dairy.", rec="2026-08-01", weak=["unconfirmed_inference"], ents=[4]),
        fact(4, "Alena asked for cottage cheese.", rec="2026-10-26", weak=[], ents=[4]),
        fact(6, "Dmitry wakes at 7.", rec="2026-10-01", weak=["no_source"], ents=[]),
        fact(7, "Dmitry wakes at 6.", rec="2026-10-02", weak=[], ents=[]),
        fact(5, "Varya went to the Sunshine kindergarten.", status="ended", to="2026-08-31", weak=[]),
    ]
    s["ENT"] = [dict(s["ENT"][0], id=3, name="Varya", type="person", type_by="relative_or_role", type_fact=1),
                dict(s["ENT"][0], id=4, name="Olga Petrovna", standing="provisional", type=None),
                dict(s["ENT"][0], id=5, name="Swimming", standing="provisional", type=None)]
    s["CONFLICTS"] = [
        {"id": "C1", "a": 1, "b": 2, "score": 0.8, "shared": [3], "since": "2026-10-25",
         "differ": {"a": ["tuesdays"], "b": ["wednesdays"], "negation": False},
         "relies": {"on": 1, "why": "weight", "sides": {"1": {"trust": 0.7, "found": True, "decay": None},
                                                        "2": {"trust": 0.4, "found": True, "decay": None}}}},
        {"id": "C2", "a": 3, "b": 4, "score": 0.6, "shared": [4], "since": "2026-10-26",
         "differ": {"a": ["dairy"], "b": ["cottage", "cheese"], "negation": True},
         "relies": {"on": None, "why": "tie", "sides": {}}},
        {"id": "C3", "a": 6, "b": 7, "score": 0.5, "shared": []},   # an exporter from before "relies"
    ]
    s["PENDING"] = ([{"kind": "conflict", "id": c["id"], "a": c["a"], "b": c["b"], "status": "open"}
                     for c in s["CONFLICTS"]]
                    + [{"kind": "merge", "id": "D-4", "keep": 1, "copy": 2, "covered": True, "extra": [],
                        "created": "2026-10-26", "asked": None, "status": "open", "current": True},
                       {"kind": "memory_conflict", "id": "M-1", "fact": 1, "entry": "Swims on Fridays.",
                        "asked": "2026-10-27", "status": "open"},
                       {"kind": "relevant", "id": "R-7", "text": "Water the flowers.", "last_mention_days": 61,
                        "asked": "2026-10-27", "status": "open"},
                       {"kind": "queue", "id": "Q", "records": 1, "writes": 2, "unreadable": 0,
                        "oldest": "2026-10-20", "status": "open"}])
    s["FEED"] = [{"d": "2026-10-27", "k": "new", "f": 4},
                 {"d": "2026-10-27", "k": "night", "at": "2026-10-27T03:05:00+07:00", "alerts": [], "stats": {}},
                 {"d": "2026-10-26", "k": "conf", "f": 1, "by": "Alena"},
                 {"d": "2026-10-25", "k": "new", "f": 2}]
    s["TOMBS"] = [{"id": 9, "d": "2026-10-25"}]
    s["MERGES"] = [{"from": 8, "into": 1, "d": "2026-10-23", "prop": "D-2", "text": "Varya swims."}]
    s["HIDDEN"] = [{"id": 10, "d": "2026-10-01", "why": "credential", "status": "current"}]
    return s


def tile(html: str, key: str) -> int:
    m = re.search(r'<a class="kpi mm-kpi[^"]*" href="#mmap/%s"><div class="v">(\d+)</div>' % key, html)
    assert m, "no tile " + key
    return int(m.group(1))


def badge(html: str, key: str) -> int:
    m = re.search(r'<a href="#mmap/%s" data-pg="%s"[^>]*>[^<]*(?:<b[^>]*>(\d+)</b>)?</a>' % (key, key), html)
    assert m, "no nav link " + key
    return int(m.group(1) or 0)


def visible(html: str) -> str:
    return re.sub(r"<[^>]+>", " ", html)


class MemoryMapPagesTests(unittest.TestCase):
    """What a person reads on the pages of the tab."""

    def setUp(self):
        i18n.set_lang("en")
        self.html = build.gen_memory_map.view(cfg_for(new_home()), rich_snapshot())

    def tearDown(self):
        i18n.set_lang("en")

    def test_one_number_means_one_thing_on_the_tile_and_the_tab(self):
        """The number on an overview tile and on the tab it leads to count the same things: «6 facts on a weak
        basis» on the tile and 8 on the tab (6 facts + 2 names) was the bug."""
        h = self.html
        self.assertEqual(tile(h, "weak"), 3, "facts 2, 3 and 6")
        for key in ("pending", "conflicts", "weak", "week", "removed"):
            self.assertEqual(tile(h, key), badge(h, key), key)
        home = visible(panel(h, "home"))
        self.assertIn("and 2 names in question", home, "the names are said apart, next to the facts")
        self.assertIn("Names guessed from one fact", panel(h, "weak"))
        self.assertEqual(tile(h, "week"), 2, "two new facts, not four events")
        self.assertEqual(tile(h, "removed"), 1, "forgotten on request")

    def test_a_question_shows_only_a_number_the_chat_takes(self):
        """D-4 is what the person answers in the chat («yes D-4»); C1, M-1, R-7 are the export's own counters that
        no command takes, so they are not shown anywhere."""
        text = visible(self.html)
        self.assertIn("D-4", text)
        for internal in ("C1", "C2", "M-1", "R-7"):
            self.assertIsNone(re.search(r"(?<![\w-])%s(?![\w-])" % re.escape(internal), text), internal)

    def test_people_topics_and_undetermined_names_are_apart(self):
        s = rich_snapshot()
        s["ENT"].append(dict(s["ENT"][0], id=6, name="Lyceum No. 3", type="topic", type_by="digit"))
        s["FACTS"][4]["ents"] = [6]
        html = build.gen_memory_map.view(cfg_for(new_home()), s)
        ents = panel(html, "ents")
        sections = {m.group(1): m.group(2) for m in re.finditer(
            r'<h2>([^<]+)</h2>(.*?)(?=<div class="sec">|$)', ents, re.S)}
        self.assertIn("Varya", visible(sections["People"]))
        self.assertNotIn("Olga Petrovna", visible(sections["People"]), "an undetermined name is not a person")
        self.assertIn("Lyceum No. 3", visible(sections["Topics and projects"]))
        self.assertIn("Olga Petrovna", visible(sections["Not determined"]))
        self.assertIn("Swimming", visible(sections["Not determined"]))
        self.assertNotIn("<h2>People and topics</h2>", ents, "no mixed section any more")

    def test_a_contradiction_shows_what_the_agent_leans_on_now(self):
        page = panel(self.html, "conflicts")
        cards = page.split('<div class="card mm-pend">')[1:]
        self.assertEqual(len(cards), 3)
        first = visible(cards[0])
        # №1 was recorded earlier but is trusted more: it is what search puts first, on the left
        self.assertLess(first.index("Leans on now"), first.index("№1"))
        self.assertLess(first.index("№1"), first.index("Disagrees"))
        self.assertLess(first.index("Disagrees"), first.index("№2"))
        self.assertIn("0.70", first)
        self.assertIn("0.40", first)
        self.assertIn("tuesdays", first)
        self.assertIn("wednesdays", first)
        # no answer about the order: earlier / later, and why it is not said
        second = visible(cards[1])
        self.assertNotIn("Leans on now", second)
        self.assertIn("Recorded earlier", second)
        self.assertIn("depends on the question", second)
        self.assertIn("negation", second)
        third = visible(cards[2])
        self.assertIn("Recorded earlier", third)

    def test_version_bars_run_from_record_to_replacement(self):
        s = rich_snapshot()

        def v(seq, st, rec, closed, text):
            return {"seq": seq, "status": st, "text": text, "from": None, "to": None, "to_exact": True, "rec": rec,
                    "closed": closed, "origin": "said", "via": "tool"}

        s["FACTS"][0]["versions"] = [v(1, "corrected", "2026-08-01", "2026-08-02", None),
                                     v(2, "changed", "2026-08-02", "2026-09-10", "Varya swims on Mondays."),
                                     v(3, "current", "2026-09-10", None, "Varya swims on Tuesdays.")]
        s["FACTS"][-1]["versions"] = [v(1, "ended", "2026-06-01", "2026-08-31", "Varya went to the kindergarten.")]
        html = build.gen_memory_map.view(cfg_for(new_home()), s)

        def bars(fid):
            page = panel(html, "f%d" % fid)
            self.assertIn('class="mm-tl"', page)
            return [(m.group(1), float(m.group(2)), float(m.group(3)), m.group(4)) for m in re.finditer(
                r'<div class="mm-bar ([\w ]+)" style="left:([\d.]+)%;width:([\d.]+)%"[^>]*>(.*?)</div>', page)]

        b = bars(1)
        self.assertEqual([x[0].split()[0] for x in b], ["corrected", "changed", "current"])
        self.assertNotIn("Mondays", b[0][3], "a corrected version shows no text")
        self.assertLess(b[0][1], b[1][1])
        self.assertLess(b[1][1], b[2][1])
        self.assertAlmostEqual(b[2][1] + b[2][2], 100, delta=0.2, msg="the current version runs to the snapshot")
        self.assertAlmostEqual(b[1][1] + b[1][2], b[2][1], delta=0.2, msg="a version ends where the next begins")
        ended = bars(5)
        self.assertLess(ended[0][1] + ended[0][2], 90, "a closed fact's bar stops where it was closed")

    def test_connections_are_drawn_around_the_name(self):
        s = rich_snapshot()
        base = s["FACTS"][0]
        s["ENT"] = [dict(s["ENT"][0], id=i, name=("<b>N%d</b>" % i), type=None) for i in range(20, 32)]
        # ten neighbours of 20 with 3 shared facts each; 21 and 22 get more; 31 shares nothing
        s["FACTS"] = [dict(base, id=100 + k, ents=[20, 21 + k % 10], weak=[], versions=[]) for k in range(30)]
        s["FACTS"] += [dict(base, id=200 + k, ents=[20, 21], weak=[], versions=[]) for k in range(4)]
        s["FACTS"] += [dict(base, id=300 + k, ents=[20, 22], weak=[], versions=[]) for k in range(2)]
        s["FACTS"] += [dict(base, id=400, ents=[31], weak=[], versions=[])]
        s["CONFLICTS"], s["PENDING"] = [], []
        html = build.gen_memory_map.view(cfg_for(new_home()), s)
        page = panel(html, "e20")
        svg = re.search(r"<svg[^>]*>.*?</svg>", page, re.S)
        self.assertIsNotNone(svg, "no drawing of the connections")
        svg = svg.group(0)
        links = re.findall(r'<a href="#mmap/e(\d+)"', svg)
        self.assertEqual(len(links), 8, "the 8 with most shared facts")
        self.assertEqual(links[:2], ["21", "22"])
        self.assertIn("+2 more", visible(page))
        widths = [float(w) for w in re.findall(r'<line[^>]*stroke-width="([\d.]+)"', svg)]
        self.assertEqual(len(widths), 8)
        self.assertEqual(widths, sorted(widths, reverse=True))
        self.assertGreater(widths[0], widths[-1])
        self.assertNotIn("<b>N2", svg)
        self.assertNotIn("<svg", panel(html, "e31"), "a name that shares nothing has no drawing")


if __name__ == "__main__":
    unittest.main()


class MemoryMapLimitTests(unittest.TestCase):
    """A store over the export's size limit (plugin 0.25.0+): the map says how much it shows, in both languages, so a
    person does not read a cut map as the whole memory. Under the limit, or from an older plugin, nothing is said."""

    def render(self, limits, lang="en"):
        snap = snapshot()
        if limits is not None:
            snap["SNAP"]["limits"] = limits
        i18n.set_lang(lang)
        try:
            return build.gen_memory_map.view(cfg_for(new_home()), snap, None)
        finally:
            i18n.set_lang("en")

    def test_a_cut_map_says_how_much_it_shows(self):
        cut = {"facts": {"max": 2, "total": 1500, "shown": 2}, "entities": {"max": 300, "total": 1, "shown": 1}}
        en = self.render(cut)
        self.assertIn("2 of 1500 facts", panel(en, "home"))
        self.assertIn("2 of 1500 facts", panel(en, "about"))
        self.assertNotIn("names", visible(panel(en, "home")).split("size limit")[1].split(".")[0])
        ru = self.render(cut, "ru")
        self.assertIn("2 из 1500 фактов", panel(ru, "home"))

    def test_a_whole_map_or_an_older_plugin_says_nothing(self):
        whole = {"facts": {"max": 1000, "total": 2, "shown": 2}, "entities": {"max": 300, "total": 1, "shown": 1}}
        for limits in (whole, None, {"facts": "garbage"}):
            with self.subTest(limits=limits):
                self.assertNotIn("size limit", self.render(limits))

    def test_a_contradiction_with_a_side_cut_by_the_limit_is_still_shown(self):
        """The tab's button counts every contradiction: each one has a card, a side the limit cut shows its number."""
        snap = snapshot(CONFLICTS=[{"id": "C-1", "a": 1, "b": 77, "shared": [3], "relies": {"on": None, "why": "tie"}}])
        snap["SNAP"]["limits"] = {"facts": {"max": 2, "total": 900, "shown": 2}}
        html = build.gen_memory_map.view(cfg_for(new_home()), snap, None)
        page = panel(html, "conflicts")
        self.assertEqual(page.count('<div class="card mm-pend">'), 1)
        self.assertIn("№77", visible(page))
        self.assertIn("size limit", visible(page))


class ExporterFailureLogTests(unittest.TestCase):
    """A failed export's log line names a kind the dashboard knows, never a line or value the exporter printed."""

    def test_an_error_line_shaped_like_an_error_name_is_not_logged(self):
        home = new_home()
        fake_plugin(home, "sys.stderr.write(%r); sys.exit(1)" % "Traceback\nValueError: x\nMyPrivateStoredSecretError\n")
        buf = io.StringIO()
        with redirect_stderr(buf):
            build.gen_memory_map.load(cfg_for(home))
        self.assertNotIn("MyPrivateStoredSecret", buf.getvalue())

    def test_an_unknown_schema_value_is_not_logged(self):
        home = new_home()
        fake_plugin(home, printing(snapshot(schema_version="Пароль hunter2")))
        buf = io.StringIO()
        with redirect_stderr(buf):
            data, why = build.gen_memory_map.load(cfg_for(home))
        self.assertIsNone(data)
        self.assertNotIn("hunter2", buf.getvalue())
        self.assertIn("memory-map failed", buf.getvalue())
