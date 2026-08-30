"""Settings page — the one part of the dashboard that is not static.

A static page cannot edit its own configuration, accept a billing export or
trigger a rebuild, so this is a tiny local server that renders **inside the same
design system** (same style.css, sidebar and tabs as the dashboard) and offers
one form field per setting instead of a raw JSON blob.

    python -m hermes_dashboard.settings [--config dashboard.json] [--host 127.0.0.1] [--port 8648]

What it can do: edit dashboard.json field by field (schema in settings_schema.py),
edit budgets.env key by key (comments preserved), upload a billing export CSV into
cache/, rebuild now, show the last build log.

Security model:
  • binds to 127.0.0.1 only; expose it through the same reverse-proxy location
    that already protects the dashboard with basic-auth (examples/nginx.conf);
  • every POST needs the CSRF token minted at start-up and a same-host Origin;
  • only paths declared in the schema are written — an unknown form field is
    ignored, so a stray name cannot inject a key into dashboard.json;
  • it never runs arbitrary shell: "rebuild" spawns one fixed command;
  • dashboard.json is validated and backed up before it is replaced;
  • uploads are capped (5 MB) and must look like a cost CSV.
"""
from __future__ import annotations

import argparse
import email.parser
import hmac
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from . import render
from .common import atomic_write, esc, read_json
from .config import LANG_CHARS, Config, child_environment, load_config, set_current
from .i18n import _, set_lang
from .settings_schema import BUDGET_HELP, SCHEMA

MAX_UPLOAD = 5 * 1024 * 1024
from .collectors.anthropic_cost import CSV_SUFFIX as CSV_NAME   # one convention, one place
KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ENV_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


# ── dotted-path helpers ─────────────────────────────────────────────────

def dig(data: dict, path: str, default=None):
    node = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def put(data: dict, path: str, value) -> None:
    parts = path.split(".")
    node = data
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def _num(s: str, kind: str):
    s = (s or "").strip().replace(" ", "").replace(",", ".")
    if s == "":
        return 0 if kind == "int" else 0.0
    try:
        return int(float(s)) if kind == "int" else float(s)
    except ValueError:
        raise ValueError(f"«{s}» is not a number")


def _csv_list(s: str) -> list[str]:
    return [p.strip() for p in re.split(r"[,\n]", s or "") if p.strip()]


# ── reading the form back into a config dict ────────────────────────────

def _langs(cfg: Config) -> list[str]:
    return cfg.languages


def apply_form(cfg: Config, data: dict, fields: dict[str, str]) -> list[str]:
    """Write every schema-declared field from the form into `data`. Returns problems."""
    errs: list[str] = []
    langs = _langs(cfg)
    for section in SCHEMA:
        for card in section["cards"]:
            for f in card["fields"]:
                try:
                    _apply_field(data, f, fields, langs)
                except ValueError as e:
                    errs.append(f'{f["path"]}: {e}')
    return errs


def _apply_field(data: dict, f: dict, fields: dict[str, str], langs: list[str]) -> None:
    path, kind = f["path"], f["kind"]
    if kind in ("text", "select"):
        if path in fields:
            put(data, path, fields[path].strip())
    elif kind in ("number", "int"):
        if path in fields:
            put(data, path, _num(fields[path], kind))
    elif kind == "bool":
        if path in fields:
            put(data, path, fields[path] in ("1", "on", "true"))
    elif kind == "csv":
        if any(k == path for k in fields):
            put(data, path, _csv_list(fields[path]))
    elif kind == "i18n":
        got = {l: fields[f"{path}.{l}"].strip() for l in langs if f"{path}.{l}" in fields}
        if got:
            cur = dig(data, path)
            merged = dict(cur) if isinstance(cur, dict) else {}
            merged.update(got)
            put(data, path, {k: v for k, v in merged.items() if v})
    elif kind == "i18n_lines":
        got = {l: [x.strip() for x in fields[f"{path}.{l}"].splitlines() if x.strip()]
               for l in langs if f"{path}.{l}" in fields}
        if got:
            cur = dig(data, path)
            merged = dict(cur) if isinstance(cur, dict) else {}
            merged.update(got)
            put(data, path, {k: v for k, v in merged.items() if v})
    elif kind == "map":
        rows = _rows(fields, f"{path}.__k")
        if rows is None:
            return
        out = {}
        for i in rows:
            k = fields.get(f"{path}.__k.{i}", "").strip()
            v = fields.get(f"{path}.__v.{i}", "").strip()
            if k:
                out[k] = v
        put(data, path, out)
    elif kind == "objects":
        rows = _rows(fields, f"{path}.{f['item'][0]['key']}")
        if rows is None:
            return
        out = []
        for i in rows:
            item = {}
            for sub in f["item"]:
                name = f"{path}.{sub['key']}.{i}"
                sk, skind = sub["key"], sub["kind"]
                if skind == "i18n":
                    got = {l: fields[f"{name}.{l}"].strip() for l in langs if f"{name}.{l}" in fields}
                    got = {k: v for k, v in got.items() if v}
                    if got:
                        item[sk] = got
                elif name in fields:
                    raw = fields[name].strip()
                    if skind in ("number", "int"):
                        if raw != "":
                            item[sk] = _num(raw, skind)
                    elif skind == "csv":
                        if raw:
                            item[sk] = _csv_list(raw)
                    elif raw:
                        item[sk] = raw
            primary = next((s["key"] for s in f["item"] if s.get("primary")), f["item"][0]["key"])
            if item.get(primary):
                out.append(item)
        put(data, path, out)


def _rows(fields: dict[str, str], prefix: str) -> list[int] | None:
    """Row indices present in the form for a repeatable block (None = block absent)."""
    idx = set()
    pref = prefix + "."
    for name in fields:
        if not name.startswith(pref):
            continue
        tail = name[len(pref):].split(".")[0]
        if tail.isdigit():
            idx.add(int(tail))
    return sorted(idx) if idx else ([] if (prefix + ".__present") in fields else None)


# ── budgets.env ─────────────────────────────────────────────────────────

def parse_env(text: str) -> list[dict]:
    """Ordered lines: {'kind': 'kv'|'raw', ...}. Comments and blanks are preserved."""
    out = []
    for line in text.splitlines():
        m = ENV_LINE_RE.match(line)
        if m:
            rest = m.group(2)
            val, _sep, comment = rest.partition("#")
            out.append({"kind": "kv", "key": m.group(1), "value": val.strip(),
                        "comment": ("#" + comment) if _sep else ""})
        else:
            out.append({"kind": "raw", "text": line})
    return out


def render_env(lines: list[dict], values: dict[str, str], extra: list[tuple[str, str]]) -> str:
    out = []
    seen = set()
    for ln in lines:
        if ln["kind"] == "raw":
            out.append(ln["text"])
            continue
        key = ln["key"]
        seen.add(key)
        val = values.get(key, ln["value"])
        pad = "   " if ln["comment"] else ""
        out.append(f"{key}={val}{pad}{ln['comment']}".rstrip())
    for key, val in extra:
        if key and key not in seen:
            out.append(f"{key}={val}")
    return "\n".join(out).rstrip("\n") + "\n"


# ── server state ────────────────────────────────────────────────────────

class State:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        set_current(cfg)
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.Lock()
        self.build_log = ""
        self.build_running = False
        self.last_build: float | None = None
        self.messages: list[tuple[str, str]] = []

    def flash(self, kind: str, text: str) -> None:
        self.messages.append((kind, text))

    def pop_flash(self) -> list[tuple[str, str]]:
        m, self.messages = self.messages, []
        return m

    # paths
    def cfg_path(self) -> Path:
        return self.cfg.path or (self.cfg.home / "dashboard" / "dashboard.json")

    def budgets_path(self) -> Path:
        return self.cfg.path_in_home(self.cfg.get("paths.budgets_env", "dashboard/budgets.env"))

    def csv_path(self) -> Path:
        # The documented override belongs to the collector — honour the same
        # name here, or the upload lands where nothing reads it.
        for var in ("HERMES_DASHBOARD_ANTHROPIC_COST_CSV", "HERMES_DASHBOARD_COST_CSV"):
            override = os.environ.get(var)
            if override:
                return Path(override)
        for p in self.cfg.get("providers.paid", []):
            cache = p.get("cost_cache")
            if cache:
                return self.cfg.home / "cache" / (str(p.get("id", "provider")) + "_" + CSV_NAME)
        return self.cfg.home / "cache" / CSV_NAME

    # actions
    def save_settings(self, fields: dict[str, str]) -> str | None:
        p = self.cfg_path()
        try:
            data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
        except ValueError as e:
            return f"the current dashboard.json is not valid JSON ({e}) — fix it on disk first"
        if not isinstance(data, dict):
            data = {}
        errs = apply_form(self.cfg, data, fields)
        if errs:
            return "; ".join(errs[:4])
        errs = Config(data).validate()
        if errs:
            return "; ".join(errs)
        return self._write_config(p, data)

    def save_config_raw(self, text: str) -> str | None:
        try:
            data = json.loads(text)
        except ValueError as e:
            return f"JSON: {e}"
        if not isinstance(data, dict):
            return "dashboard.json must be a JSON object"
        errs = Config(data).validate()
        if errs:
            return "; ".join(errs)
        return self._write_config(self.cfg_path(), data)

    def _write_config(self, p: Path, data: dict) -> str | None:
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.is_file():
            p.with_suffix(".json.bak").write_bytes(p.read_bytes())
        _atomic(p, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
        self.cfg = load_config(str(p))
        set_current(self.cfg)
        return None

    def save_budgets(self, fields: dict[str, str]) -> str | None:
        p = self.budgets_path()
        text = p.read_text(encoding="utf-8") if p.is_file() else ""
        lines = parse_env(text)
        values = {}
        for ln in lines:
            if ln["kind"] != "kv":
                continue
            name = "budget." + ln["key"]
            if name in fields:
                v = fields[name].strip()
                if "\n" in v or "\r" in v:
                    return f"{ln['key']}: a value cannot span lines"
                values[ln["key"]] = v
        extra = []
        nk, nv = fields.get("budget.__newkey", "").strip(), fields.get("budget.__newval", "").strip()
        if nk:
            if not KEY_RE.match(nk):
                return f"«{nk}» is not a valid variable name (A–Z, digits, underscore)"
            extra.append((nk, nv))
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.is_file():
            p.with_suffix(".env.bak").write_bytes(p.read_bytes())
        _atomic(p, render_env(lines, values, extra))
        return None

    def save_csv(self, data: bytes, filename: str) -> str | None:
        if len(data) > MAX_UPLOAD:
            return f"file too large (> {MAX_UPLOAD // 1024 // 1024} MB)"
        head = data[:4096].decode("utf-8", "ignore").lower()
        if "cost" not in head or "," not in head:
            return "this does not look like a cost export CSV (no «cost» column in the header)"
        atomic_write(self.csv_path(), data)
        return None

    def rebuild(self) -> None:
        if self.build_running:
            return
        self.build_running = True

        def run():
            cmd = [sys.executable, "-m", "hermes_dashboard.build", "--config", str(self.cfg_path())]
            env = child_environment(self.cfg.home, {"PYTHONPATH": str(Path(__file__).parent.parent)})
            try:
                r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600)
                self.build_log = f"$ {' '.join(cmd)}\nrc={r.returncode}\n" + (r.stdout + r.stderr)[-6000:]
            except (OSError, subprocess.SubprocessError) as e:
                self.build_log = f"build failed to start: {e}"
            self.last_build = time.time()
            self.build_running = False

        threading.Thread(target=run, daemon=True).start()


def _atomic(p: Path, text: str) -> None:
    atomic_write(p, text)


def _age_txt(p: Path) -> str:
    if not p.is_file():
        return _("no file yet")
    d = (time.time() - p.stat().st_mtime) / 86400
    return _("today") if d < 1 else _("{d} days old").format(d=f"{d:.0f}")


# ── form widgets (design-system markup) ─────────────────────────────────

def _fld(label: str, control: str, help_txt: str = "", unit: str = "", cls: str = "") -> str:
    u = f'<span class="funit">{esc(unit)}</span>' if unit else ""
    h = f'<div class="fhint">{help_txt}</div>' if help_txt else ""
    return (f'<div class="fld {cls}"><label>{esc(label)}</label>'
            f'<div class="fctl">{control}{u}</div>{h}</div>')


def _input(name: str, value, kind: str = "text", placeholder: str = "") -> str:
    t = "number" if kind in ("number", "int") else "text"
    step = ' step="any"' if kind == "number" else (' step="1"' if kind == "int" else "")
    ph = f' placeholder="{esc(placeholder)}"' if placeholder else ""
    return (f'<input type="{t}"{step} name="{esc(name)}" value="{esc("" if value is None else value)}"{ph}>')


def _textarea(name: str, value: str, rows: int = 3) -> str:
    return f'<textarea name="{esc(name)}" rows="{rows}">{esc(value)}</textarea>'


def _select(name: str, value, options) -> str:
    opts = "".join(
        f'<option value="{esc(v)}"{" selected" if str(value) == str(v) else ""}>{esc(t)}</option>'
        for v, t in options)
    return f'<select name="{esc(name)}">{opts}</select>'


def _checkbox(name: str, value: bool) -> str:
    return (f'<input type="hidden" name="{esc(name)}" value="0">'
            f'<label class="sw"><input type="checkbox" name="{esc(name)}" value="1"'
            f'{" checked" if value else ""}><span></span></label>')


def _sub_control(name: str, sub: dict, value, langs: list[str]) -> str:
    kind = sub["kind"]
    if kind == "i18n":
        cur = value if isinstance(value, dict) else ({langs[0]: value} if value else {})
        out = ""
        for l in langs:
            out += '<span class="minilang">' + esc(l) + "</span>" + _input(name + "." + l, cur.get(l, ""))
        return out
    if kind == "select":
        return _select(name, value, sub["options"])
    if kind == "csv":
        return _input(name, ", ".join(value or []) if isinstance(value, list) else (value or ""))
    return _input(name, value, kind, sub.get("placeholder", ""))


def _delbtn() -> str:
    title = esc(_("Remove row"))
    return '<button type="button" class="rowdel" title="' + title + '">\u00d7</button>'


def _orow(cells: str) -> str:
    return '<div class="orow">' + cells + _delbtn() + '</div>'


def _rowset(path: str, present_key: str, grid: str, head: str, rows: str, tmpl: str,
            next_i: int, add_label: str) -> str:
    hidden = '<input type="hidden" name="' + esc(path) + "." + esc(present_key) + '.__present" value="1">'
    return ('<div class="rowset" data-path="' + esc(path) + '" data-next="' + str(next_i) + '" '
            'style="--cols:' + esc(grid) + '">' + hidden
            + '<div class="orow ohead">' + head + '</div><div class="obody">' + rows + '</div>'
            + "<template>" + tmpl + "</template>"
            + '<button type="button" class="addrow">+ ' + esc(add_label) + "</button></div>")


def _objects(path: str, f: dict, cur, langs: list[str]) -> str:
    item = f["item"]
    grid = ";".join(sub.get("width", "1fr") for sub in item)
    head = "".join("<span>" + esc(_(sub["label"])) + "</span>" for sub in item) + "<span></span>"
    values = cur if isinstance(cur, list) else []
    rows = ""
    for i, obj in enumerate(values):
        cells = ""
        for sub in item:
            name = path + "." + sub["key"] + "." + str(i)
            cells += '<div class="rc">' + _sub_control(name, sub, (obj or {}).get(sub["key"]), langs) + "</div>"
        rows += _orow(cells)
    tmpl_cells = ""
    for sub in item:
        tmpl_cells += ('<div class="rc">'
                       + _sub_control(path + "." + sub["key"] + ".__I__", sub, "", langs) + "</div>")
    return _rowset(path, item[0]["key"], grid, head, rows, _orow(tmpl_cells), len(values),
                   _(f.get("add_label", "Add row")))


def _map(path: str, f: dict, cur, _langs) -> str:
    head = ("<span>" + esc(_(f.get("key_label", "Key"))) + "</span><span>"
            + esc(_(f.get("val_label", "Value"))) + "</span><span></span>")
    items = list(cur.items()) if isinstance(cur, dict) else []
    rows = ""
    for i, (k, v) in enumerate(items):
        cells = ('<div class="rc">' + _input(path + ".__k." + str(i), k) + "</div>"
                 + '<div class="rc">' + _input(path + ".__v." + str(i), v) + "</div>")
        rows += _orow(cells)
    tmpl_cells = ('<div class="rc">' + _input(path + ".__k.__I__", "") + "</div>"
                  + '<div class="rc">' + _input(path + ".__v.__I__", "") + "</div>")
    return _rowset(path, "__k", "1fr 1.4fr", head, rows, _orow(tmpl_cells), len(items),
                   _(f.get("add_label", "Add row")))


def render_field(cfg: Config, data: dict, f: dict, langs: list[str]) -> str:
    path, kind = f["path"], f["kind"]
    cur = dig(data, path, cfg.get(path))
    label, help_txt, unit = _(f["label"]), _(f["help"]) if f.get("help") else "", f.get("unit", "")
    narrow = "narrow" if f.get("narrow") else ""
    if kind in ("text",):
        return _fld(label, _input(path, cur, "text", f.get("placeholder", "")), help_txt, unit, narrow)
    if kind in ("number", "int"):
        return _fld(label, _input(path, cur, kind), help_txt, unit, "narrow")
    if kind == "select":
        return _fld(label, _select(path, cur, [(v, _(t)) for v, t in f["options"]]), help_txt, unit)
    if kind == "bool":
        return _fld(label, _checkbox(path, bool(cur)), help_txt, "", "row")
    if kind == "csv":
        val = ", ".join(cur) if isinstance(cur, list) else (cur or "")
        return _fld(label, _input(path, val), help_txt, unit)
    if kind == "i18n":
        cur = cur if isinstance(cur, dict) else {}
        ctl = ""
        for l in langs:
            box = (_textarea(path + "." + l, cur.get(l, ""), 3) if f.get("textarea")
                   else _input(path + "." + l, cur.get(l, "")))
            ctl += '<div class="langrow"><span class="minilang">' + esc(l) + "</span>" + box + "</div>"
        return _fld(label, ctl, help_txt)
    if kind == "i18n_lines":
        cur = cur if isinstance(cur, dict) else {}
        ctl = ""
        for l in langs:
            box = _textarea(path + "." + l, "\n".join(cur.get(l, []) or []), 4)
            ctl += '<div class="langrow"><span class="minilang">' + esc(l) + "</span>" + box + "</div>"
        return _fld(label, ctl, help_txt or _("One item per line."))
    if kind in ("objects", "map"):
        hint = '<div class="fhint">' + help_txt + "</div>" if help_txt else ""
        inner = _objects(path, f, cur, langs) if kind == "objects" else _map(path, f, cur, langs)
        return '<div class="fld wide"><label>' + esc(label) + "</label>" + hint + inner + "</div>"
    return ""


# ── page ────────────────────────────────────────────────────────────────

def _csrf(state: State, lang: str, action: str) -> str:
    # Keep every request-derived value in the HTML attribute context escaped;
    # the language should already be allowlisted, but this is defense in depth.
    return (f'<input type="hidden" name="_csrf" value="{esc(state.token)}">'
            f'<input type="hidden" name="action" value="{esc(action)}">'
            f'<input type="hidden" name="lang" value="{esc(lang)}">')


def build_page(state: State, lang: str, host: dict) -> str:
    cfg = state.cfg
    langs = cfg.languages
    p = state.cfg_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    raw_text = p.read_text(encoding="utf-8") if p.is_file() else "{}\n"
    e = esc

    flashes = ""
    for kind, text in state.pop_flash():
        flashes += '<div class="flash ' + kind + '">' + text + "</div>"

    # ── status tiles ────────────────────────────────────────────────
    csvp = state.csv_path()
    paid = cfg.get("providers.paid", []) or []
    cost_json = None
    for prov in paid:
        if prov.get("cost_cache"):
            cost_json = cfg.path_in_home(prov["cost_cache"])
            break
    cj = (read_json(cost_json) or {}) if cost_json else {}
    age = cj.get("data_age_days")
    fresh = cfg.get("providers.cost_fresh_days", 6)
    if not cj:
        cost_cls, cost_val, cost_sub = "warn", _("no data"), _("upload an export below")
    elif isinstance(age, (int, float)) and age <= fresh:
        cost_cls, cost_val = "ok", _("real $")
        cost_sub = _("data through {d} · {n} days old").format(d=cj.get("data_through", "—"), n=age)
    else:
        cost_cls, cost_val = "warn", _("estimate")
        cost_sub = _("export ends {d} — too old to use").format(d=cj.get("data_through", "—"))
    last_build = datetime.fromtimestamp(state.last_build).strftime("%H:%M:%S") if state.last_build else "—"
    build_val = _("running…") if state.build_running else last_build

    def tile(cls, label, value, sub, size=17):
        return ('<div class="kpi ' + cls + '"><div class="l">' + e(label) + '</div><div class="v" '
                'style="font-size:' + str(size) + 'px">' + e(value) + '</div><div class="x">' + e(sub) + "</div></div>")

    tiles = (tile("ok", _("Configuration"), p.name, str(p.parent), 15)
             + tile(cost_cls, _("Money source"), cost_val, cost_sub)
             + tile("info", _("Manual rebuild"), build_val, _("cron keeps rebuilding on its own schedule"))
             + tile("", _("Output directory"), cfg.out_dir.name, str(cfg.out_dir), 15))

    def intro(text):
        return '<div class="algo" style="border-top:0;padding-top:0;margin-top:0">' + text + "</div>"

    # ── data and rebuild ────────────────────────────────────────────
    upload_txt = _("Money is shown by the best available source. An export from the provider console is the only "
                   "way to see real $: drop the CSV here and it lands in the cache that the paid-provider card "
                   "reads. While its newest day is inside the freshness window the card prints real money; when "
                   "it falls behind, the page drops back to an estimate and says so — an old export is never "
                   "passed off as today.")
    upload_card = (
        '<div class="card"><h3>' + e(_("Billing export (CSV)")) + "</h3>" + intro(upload_txt)
        + '<form method="post" enctype="multipart/form-data">' + _csrf(state, lang, "upload_csv")
        + '<div class="fld"><label>' + e(_("CSV file")) + '</label><div class="fctl">'
        + '<input type="file" name="csv" accept=".csv,text/csv" required></div>'
        + '<div class="fhint">' + e(_("Saved as")) + " <code>" + e(csvp.name) + "</code> · "
        + e(_age_txt(csvp)) + "</div></div>"
        + '<button class="btn">' + e(_("Upload")) + "</button></form></div>")

    rebuild_txt = _("Runs the generator once with the settings saved on disk. Saving a form does not rebuild by "
                    "itself — the cron does that on its schedule; this button is for seeing a change immediately.")
    rebuild_card = (
        '<div class="card"><h3>' + e(_("Rebuild")) + "</h3>" + intro(rebuild_txt)
        + '<form method="post">' + _csrf(state, lang, "rebuild")
        + '<button class="btn">' + e(_("Rebuild now")) + "</button></form>"
        + '<details style="margin-top:12px"><summary>' + e(_("last build log")) + "</summary>"
        + '<pre class="logbox">' + e(state.build_log or _("no build yet")) + "</pre></details></div>")

    # ── budgets.env ─────────────────────────────────────────────────
    bp = state.budgets_path()
    env_lines = parse_env(bp.read_text(encoding="utf-8") if bp.is_file() else "")
    bfields = ""
    for ln in env_lines:
        if ln["kind"] != "kv":
            continue
        unit, help_txt = BUDGET_HELP.get(ln["key"], ("", ""))
        hint = _(help_txt) if help_txt else e(ln["comment"].lstrip("# ").strip())
        bfields += _fld(ln["key"], _input("budget." + ln["key"], ln["value"]), hint, unit, "narrow")
    newkv = _input("budget.__newkey", "", "text", "NEW_KEY") + _input("budget.__newval", "", "text", "value")
    bfields += ('<div class="fld wide"><label>' + e(_("Add a variable")) + '</label><div class="fctl newkv">'
                + newkv + '</div><div class="fhint">'
                + e(_("Appended to the end of the file; the name must be A–Z, digits and underscores."))
                + "</div></div>")
    budget_txt = _("A plain shell file of <code>KEY=value</code> lines, shared by the dashboard and by any alert "
                   "cron you run, so both speak the same numbers. These are <b>reference points, not limits</b>: "
                   "nothing here throttles the agent — they set the scale of the weekly bar and the threshold at "
                   "which the quota alert speaks up. Saving rewrites only the values; comments and their order "
                   "stay as they are, and unknown keys are left untouched.")
    budgets_card = (
        '<div class="card full"><h3>' + e(_("Budget file")) + " · budgets.env</h3>" + intro(budget_txt)
        + '<form method="post">' + _csrf(state, lang, "save_budgets")
        + '<div class="fset">' + bfields + "</div>"
        + '<button class="btn">' + e(_("Save budget file")) + "</button></form>"
        + '<div class="fhint" style="margin-top:10px">' + e(str(bp)) + " · " + e(_age_txt(bp)) + "</div></div>")

    # ── schema-driven sections, one save form each ──────────────────
    body = ""
    for section in SCHEMA:
        cards = ""
        for card in section["cards"]:
            head = intro(_(card["intro"])) if card.get("intro") else ""
            wide = any(fld["kind"] in ("objects", "map", "i18n_lines") for fld in card["fields"])
            inner = ""
            for fld in card["fields"]:
                inner += render_field(cfg, data, fld, langs)
            cards += ('<div class="card' + (" full" if wide else "") + '"><h3>' + e(_(card["title"])) + "</h3>"
                      + head + '<div class="fset">' + inner + "</div></div>")
        body += ('<div class="sec" id="s-' + section["id"] + '"><div class="sec-h"><h2>'
                 + e(_(section["title"])) + '</h2><span class="ln"></span><span class="note">'
                 + e(_(section["note"])) + "</span></div>"
                 + '<form method="post">' + _csrf(state, lang, "save_settings")
                 + '<div class="grid g2">' + cards + "</div>"
                 + '<div class="savebar"><button class="btn">' + e(_("Save this section"))
                 + '</button><span class="fhint">' + e(_("Only the fields of this section are written."))
                 + "</span></div></form></div>")

    nav = ""
    for sect in SCHEMA:
        nav += '<a class="fbtn" href="#s-' + sect["id"] + '">' + e(_(sect["title"])) + "</a>"

    raw_txt = _("The form above writes only the fields it knows. Everything else — and any key added by a newer "
                "engine version — can be edited here. Validated on save; the previous version is kept as "
                "<code>dashboard.json.bak</code>.")
    advanced = (
        '<div class="sec"><div class="sec-h"><h2>' + e(_("Raw file")) + '</h2><span class="ln"></span>'
        '<span class="note">' + e(_("for anything the form does not cover")) + "</span></div>"
        + '<div class="card full"><h3>dashboard.json</h3>' + intro(raw_txt)
        + '<form method="post">' + _csrf(state, lang, "save_config_raw")
        + '<textarea class="mono" name="text" rows="18">' + e(raw_text) + "</textarea>"
        + '<div class="savebar"><button class="btn ghost">' + e(_("Save raw JSON")) + "</button></div>"
        + "</form></div></div>")

    intro_txt = _("Everything the pages cannot collect by themselves: who the agent is, which providers cost "
                  "money and at which tariff, which cron jobs are watchdogs. No number on the dashboard comes "
                  "from here — these settings only tell the collectors how to read and label what they find.")
    title = str(cfg.get("agent.name", "Hermes")) + " — " + _("dashboard settings")
    page = [
        render.head(cfg, title, lang, base="settings"),
        render.rail(cfg, lang, "", host["dot"], host["gwt"], host["sync"], host["sha"], host["commit"],
                    current_page="settings", link_prefix="../"),
        '<main class="work"><div class="pad">',
        ('<div class="vh"><div class="kick">' + e(_("Settings")) + " · " + e(p.name) + "</div><h1>"
         + e(_("Dashboard settings")) + "</h1><p>" + e(intro_txt) + '</p><div class="meta">'
         + e(_("served on")) + " <b>127.0.0.1</b> · " + e(_("behind the same basic-auth as the dashboard"))
         + "</div></div>"),
        flashes,
        '<div class="kpis prime">' + tiles + "</div>",
        '<div class="controls">' + nav + "</div>",
        ('<div class="sec"><div class="sec-h"><h2>' + e(_("Data and rebuild")) + '</h2><span class="ln"></span>'
         '<span class="note">' + e(_("the two things that change what the pages show right now")) + "</span></div>"
         + '<div class="grid g2">' + upload_card + rebuild_card + budgets_card + "</div></div>"),
        body,
        advanced,
        ("<footer><span>" + e(_("Settings")) + " · " + e(str(p)) + '</span><span><a href="../">'
         + e(_("back to the dashboard")) + "</a></span></footer>"),
        "</div></main>",
        render.bottom_tabs(cfg, lang, "", current_page="settings", link_prefix="../"),
        render.script(),
        "<script>" + FORM_JS + "</script></body></html>",
    ]
    return "\n".join(page)


FORM_JS = """
(function(){
  document.addEventListener("click", function(e){
    var add = e.target.closest(".addrow");
    if(add){
      var set = add.closest(".rowset"), i = parseInt(set.dataset.next || "0", 10);
      var html = set.querySelector("template").innerHTML.split("__I__").join(String(i));
      var wrap = document.createElement("div");
      wrap.innerHTML = html;
      set.querySelector(".obody").appendChild(wrap.firstElementChild);
      set.dataset.next = String(i + 1);
      return;
    }
    var del = e.target.closest(".rowdel");
    if(del){ del.closest(".orow").remove(); }
  });
  document.querySelectorAll("form").forEach(function(f){
    var btn = f.querySelector("button.btn");
    if(!btn) return;
    f.addEventListener("submit", function(){ btn.disabled = true; btn.classList.add("busy"); });
  });
})();
"""


# ── HTTP ────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "hermes-dashboard-settings"
    state: State

    def log_message(self, fmt, *args):
        sys.stderr.write("settings: " + (fmt % args) + "\n")

    def _send(self, body: str, status: int = 200, ctype: str = "text/html; charset=utf-8", extra=None):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Robots-Tag", "noindex")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        # The page currently has inline style/script blocks. Keep those working,
        # while restricting every other resource and all navigation targets.
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'; "
            "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' "
            "https://fonts.googleapis.com; img-src 'self' data:; "
            "font-src 'self' https://fonts.gstatic.com; connect-src 'self'",
        )
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _same_host(self) -> bool:
        origin = self.headers.get("Origin") or self.headers.get("Referer") or ""
        host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or ""
        if not origin:
            return True
        try:
            origin_host = urlparse(origin).hostname
            request_host = urlparse("//" + host).hostname
        except ValueError:
            return False
        return bool(origin_host and request_host
                    and origin_host.rstrip(".").lower() == request_host.rstrip(".").lower())

    def _lang(self) -> str:
        """Active language — ONLY ever one of cfg.languages.

        ?lang= and the cookie are attacker-controlled: the value ends up in
        <html lang>, in hidden form fields and in a Set-Cookie header, so an
        unchecked string is reflected XSS (and header injection) against an
        already-authenticated admin. Whitelisting is the fix; escaping in the
        renderer is the second line of defence.
        """
        allowed = self.state.cfg.languages
        q = parse_qs(urlparse(self.path).query)
        cand = (q.get("lang") or [None])[0]
        if cand not in allowed:
            m = re.search("hd-lang=(" + LANG_CHARS + ")", self.headers.get("Cookie") or "")
            cand = m.group(1) if m else None
        return cand if cand in allowed else self.state.cfg.default_lang

    def _host_info(self) -> dict:
        from . import sysinfo
        gw = sysinfo.gateway_state()
        sha, commit = sysinfo.git_head()
        return {"dot": "var(--ok)" if gw == "active" else "var(--no)",
                "gwt": _("running") if gw == "active" else gw,
                "sync": sysinfo.last_sync(), "sha": sha, "commit": commit}

    def _https_request(self) -> bool:
        """Whether the trusted proxy presented this request over HTTPS."""
        return (self.headers.get("X-Forwarded-Proto") or "").split(",", 1)[0].strip().lower() == "https"

    # set_lang() is process-wide state and ThreadingHTTPServer answers requests
    # in parallel: without this, two browsers on different languages hand each
    # other half-translated pages. The page is cheap, so one lock is enough.
    _render_lock = threading.Lock()

    def do_GET(self):
        path = urlparse(self.path).path
        with self._render_lock:
            return self._do_get_locked(path)

    def _do_get_locked(self, path):
        lang = self._lang()
        set_lang(lang, allowed=self.state.cfg.languages, default=self.state.cfg.default_lang)
        if path.endswith("/log"):
            return self._send(self.state.build_log or _("no build yet"), ctype="text/plain; charset=utf-8")
        if path.endswith("/health"):
            return self._send(json.dumps({"ok": True, "building": self.state.build_running}),
                              ctype="application/json")
        return self._send(build_page(self.state, lang, self._host_info()),
                          extra={"Set-Cookie": f"hd-lang={lang}; Path=/; HttpOnly; SameSite=Lax"
                                 + ("; Secure" if self._https_request() else "")})

    def do_POST(self):
        with self._render_lock:
            return self._do_post_locked()

    def _do_post_locked(self):
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else 0
        except (TypeError, ValueError):
            return self._send("invalid Content-Length", 400, "text/plain; charset=utf-8")
        if length < 0:
            return self._send("invalid Content-Length", 400, "text/plain; charset=utf-8")
        if length > MAX_UPLOAD + 262144:
            return self._send("payload too large", 413, "text/plain")
        raw = self.rfile.read(length)
        ctype = self.headers.get("Content-Type") or ""
        fields: dict[str, str] = {}
        files: dict[str, tuple[str, bytes]] = {}
        if ctype.startswith("multipart/form-data"):
            msg = email.parser.BytesParser().parsebytes(
                b"Content-Type: " + ctype.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + raw)
            for part in msg.get_payload() if msg.is_multipart() else []:
                name = part.get_param("name", header="content-disposition")
                fname = part.get_param("filename", header="content-disposition")
                payload = part.get_payload(decode=True) or b""
                if fname:
                    files[name] = (fname, payload)
                elif name:
                    fields[name] = payload.decode("utf-8", "replace")
        else:
            for k, v in parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True).items():
                fields[k] = v[-1]          # checkbox: hidden "0" first, checked "1" wins
        if not hmac.compare_digest(fields.get("_csrf", ""), self.state.token) or not self._same_host():
            return self._send(_("Request rejected: bad or missing CSRF token."), 403, "text/plain; charset=utf-8")
        lang = set_lang(fields.get("lang") or self._lang(),
                        allowed=self.state.cfg.languages,
                        default=self.state.cfg.default_lang)
        st = self.state
        action = fields.get("action", "")
        with st.lock:
            if action == "save_settings":
                err = st.save_settings(fields)
                st.flash("bad" if err else "ok", err or _("Settings saved. Rebuild to see them on the dashboard."))
            elif action == "save_config_raw":
                err = st.save_config_raw(fields.get("text", ""))
                st.flash("bad" if err else "ok", err or _("dashboard.json saved (backup: dashboard.json.bak)."))
            elif action == "save_budgets":
                err = st.save_budgets(fields)
                st.flash("bad" if err else "ok", err or _("Budget file saved."))
            elif action == "upload_csv":
                f = files.get("csv")
                if not f or not f[1]:
                    st.flash("bad", _("No file received."))
                else:
                    err = st.save_csv(f[1], f[0])
                    st.flash("bad" if err else "ok",
                             err or _("Export saved as {p}. Rebuild to see real $ on the dashboard.").format(
                                 p=st.csv_path().name))
            elif action == "rebuild":
                st.rebuild()
                st.flash("ok", _("Rebuild started — refresh this page in a few seconds and check the log."))
            else:
                st.flash("bad", _("Unknown action."))
        self.send_response(303)
        # The value is allowlisted above, then encoded as a query parameter.
        # Never interpolate request data into a response header.
        self.send_header("Location", (urlparse(self.path).path or "/") + "?" + urlencode({"lang": lang}))
        self.end_headers()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="hermes-dashboard settings")
    ap.add_argument("--config")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8648)
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    Handler.state = State(cfg)
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    sys.stderr.write(f"settings: listening on http://{a.host}:{a.port}/ (config {cfg.path})\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
