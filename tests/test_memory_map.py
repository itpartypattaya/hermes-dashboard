"""The memory map tab (gen_memory_map.py) on a fake exporter — no plugin, no agent needed.

Requirements, in the owner's words, each a test below:
- a dashboard that does not turn the tab on is the dashboard it was: no tab, no link, nothing spawned;
- turned on, the tab sits in the sidebar and the phone tabs like the others, the Memory view links to it, and it
  speaks both languages;
- a broken export (crash, garbage, wrong format, hang, missing plugin or store) costs only this tab: it says it is
  unavailable instead of showing numbers, the log gets a "[section] memory-map failed" line, the build goes on;
- memory text is written from chat messages, so markup in it is shown as text, never run;
- the tab shows what the export decided (trust words, what is weak, the health category) without re-deciding;
- the exporter is asked for the configured store, in the dashboard's zone.

The snapshots below are shaped like the plugin's real output (schema_version 1, docs/memory-map.md there),
including its dirty cases: an old record with no source, a corrected version with no text, a hidden version."""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

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
    env = dict(os.environ, HERMES_HOME=str(home), PYTHONPATH=str(ROOT))
    r = subprocess.run([sys.executable, "-m", "hermes_dashboard.build", "--config", str(cfgp), "--out", str(out)],
                       env=env, capture_output=True, text=True, encoding="utf-8", timeout=180)
    if r.returncode != 0:
        raise AssertionError("build failed: " + r.stderr[-2000:])
    return {p.name: p.read_text(encoding="utf-8") for p in out.glob("*.html")}, r.stderr


class MemoryMapTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_lang("en")

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
            for needle in ("v-mmap", "mmap", "mm-pg", "Memory map", "Карта памяти"):
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


if __name__ == "__main__":
    unittest.main()
