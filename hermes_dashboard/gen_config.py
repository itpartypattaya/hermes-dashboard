"""Live config map — the «Config» view.

Two layers: MEANING (why a key is set the way it is) lives in a hand-written
descriptions JSON (paths.config_descriptions[lang]); VALUES (default / effective /
overridden / drift) are computed on every run — always fresh.

Three read-only sources: 1) defaults — DEFAULT_CONFIG from the *running* Hermes core
(needs the Hermes venv python: paths.venv_python), 2) the effective config.yaml,
3) the source of truth — `<truth_ref>:config.yaml` (config_map.truth_ref, e.g.
origin/main). (2) vs (1) = overridden; (2) vs (3) = DRIFT: the key acts now but dies
when the working copy is reset to the truth ref. Drift is computed against the local
ref; no fetch is done — the ref age is printed instead.

Before printing, a self-check runs; on failure nothing is printed and rc=1, so the
build substitutes the last good version from cache/config-map.html. A broken page
with HTTP 200 is the most expensive failure: neither curl nor nginx see it.
"""
from __future__ import annotations

import fnmatch
import html
import json
import os
import re
import subprocess
import tempfile
import sys

from .config import current
from .i18n import _, lang

_cfg = current()
HOME = str(_cfg.home)
CORE = os.path.join(HOME, "hermes-agent")
LIVE_PATH = os.path.join(HOME, "config.yaml")


def descr_path() -> str:
    """Descriptions file for the active language (paths.config_descriptions[lang])."""
    d = _cfg.get("paths.config_descriptions", {}) or {}
    return str(_cfg.path_in_home(_cfg.text(d, lang()))) if d else ""


def cache_path() -> str:
    return os.path.join(HOME, "cache", f"config-map.{lang()}.html")


TRUTH_REF = os.environ.get("HERMES_DASHBOARD_TRUTH_REF") or str(_cfg.get("config_map.truth_ref", "origin/main"))
NODE = os.path.expanduser(str(_cfg.get("paths.node", "") or ""))

# Keys the engine stores under one name while the gateway persists them under another.
# Without this table the detector would mistake normalisation for a loss: in DEFAULT_CONFIG
# `model` is a string, in the saved config it is a dict model.{default,provider,base_url}.
ALIASES = {"model.default": "model"}

# Keys the engine writes on its own. They can never appear in the truth ref, so a
# difference here is not a loss and must not be flagged red: otherwise the map would
# cry wolf forever and nobody would read it.
ENGINE_WRITTEN = ("agent.personalities.", "onboarding.seen.", "_config_version")

# Our schema extensions: the engine reads them with inline defaults; they are absent from DEFAULT_CONFIG.
OURS = ("mcp_servers.", "plugins.", "platforms.", "platform_toolsets.")

# Secrets are filtered by PATH NAME, not by value, and set up in advance as an
# invariant: today no such keys reach the page, but the config grows and the page is
# publicly addressable. Over-masking is safer than under-masking.
SECRET_PATH_RE = re.compile(
    r"(api_?key|password|passwd|secret|token|credential|authorization|session_string)", re.I
)
# Second line of defence: the value looks like a live secret regardless of the key name.
SECRET_VALUE_RE = re.compile(
    r"(ctk_[A-Za-z0-9]{8}|github_pat_[A-Za-z0-9_]{8}|ghp_[A-Za-z0-9]{16}|sk-[A-Za-z0-9]{16}"
    r"|xox[baprs]-[A-Za-z0-9-]{10}|AIza[0-9A-Za-z_\-]{20})"
)
MASK = "‹hidden›"

# Cap on value length per cell. A table column is as wide as its LONGEST value
# (allowlists, fallback chains) — without truncation the value column balloons and
# squeezes the descriptions the map exists for. CSS cannot fix this: max-width on
# a cell has no effect under table-layout:auto. The full value stays in the cell title.
VALUE_MAX = 38

STATUS_LABEL = {
    "over": ("overridden", "over"),
    "def": ("default", "def"),
    "ext": ("outside schema", "ext"),
    "unset": ("not set", "unset"),
}
DRIFT_LABEL = {
    "changed": "drift · value differs from the truth ref",
    "new": "drift · key absent in the truth ref, dies on reset",
    "lost": "present in the truth ref, missing in the live config",
    "auto": "written by the engine itself — not a loss",
}


def die(msg: str) -> None:
    sys.stderr.write(f"gen_config: {msg}\n")
    sys.exit(1)


def esc(s) -> str:
    return html.escape(str(s), quote=True)


# ─────────────────────────── sources ───────────────────────────

def load_defaults() -> dict:
    """Defaults from the running core's code. Without them the map is meaningless — bail out."""
    sys.path.insert(0, CORE)
    try:
        from hermes_cli.config import DEFAULT_CONFIG  # type: ignore
    except Exception as e:  # noqa: BLE001 — the reason matters more than the type
        die(f"cannot import DEFAULT_CONFIG from {CORE}: {e}")
    return DEFAULT_CONFIG  # type: ignore[return-value]


def load_live() -> dict:
    import yaml

    with open(LIVE_PATH, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        die(f"{LIVE_PATH} did not parse into a mapping")
    return data


def git(*args: str) -> tuple[int, str]:
    try:
        r = subprocess.run(["git", "-C", HOME, *args], capture_output=True,
                           text=True, timeout=25)
    except Exception as e:  # noqa: BLE001
        return 1, str(e)
    return r.returncode, r.stdout


def load_truth() -> tuple[dict | None, str]:
    """Truth ref + its age. Empty ref → the map works without the drift column."""
    import yaml

    rc, out = git("show", f"{TRUTH_REF}:config.yaml")
    if rc != 0:
        return None, f"{TRUTH_REF} " + _("unavailable")
    try:
        data = yaml.safe_load(out)
    except Exception as e:  # noqa: BLE001
        return None, f"{TRUTH_REF} " + _("did not parse") + f": {e}"
    rc2, meta = git("log", "-1", "--format=%h · %cd", "--date=format-local:%Y-%m-%d %H:%M", TRUTH_REF)
    return (data if isinstance(data, dict) else None), (meta.strip() if rc2 == 0 else TRUTH_REF)


def flatten(node, prefix: str = "") -> dict:
    """{a:{b:1}} → {"a.b":1}. All further comparison works on string paths.

    An empty dict/list is a meaningful value ("nothing configured"), not a reason
    to recurse deeper: otherwise the key would simply vanish from the map.
    """
    if not isinstance(node, dict):
        return {prefix: node}
    out: dict = {}
    for k, v in node.items():
        path = f"{prefix}.{k}" if prefix else str(k)
        if isinstance(v, dict) and v:
            out.update(flatten(v, path))
        else:
            out[path] = v
    return out


def mask(path: str, value):
    if SECRET_PATH_RE.search(path):
        return MASK
    if isinstance(value, str) and SECRET_VALUE_RE.search(value):
        return MASK
    if isinstance(value, (list, dict)):
        dumped = json.dumps(value, ensure_ascii=False)
        if SECRET_VALUE_RE.search(dumped):
            return MASK
    return value


def fmt(value) -> str:
    if value is MASK:
        return MASK
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        if not value:
            return "[]" if isinstance(value, list) else "{}"
        return json.dumps(value, ensure_ascii=False)
    if value == "":
        return _("«empty»")
    return str(value)


def is_engine_written(path: str) -> bool:
    return any(path == p or path.startswith(p) for p in ENGINE_WRITTEN)


# ─────────────────────────── row model ───────────────────────────

def build_rows(defaults: dict, live: dict, truth: dict | None) -> list[dict]:
    rev = {v: k for k, v in ALIASES.items()}
    D = {rev.get(k, k): v for k, v in flatten(defaults).items()}
    L = flatten(live)
    T = {rev.get(k, k): v for k, v in flatten(truth).items()} if truth is not None else None

    paths = set(D) | set(L) | (set(T) if T is not None else set())
    rows = []
    for p in sorted(paths):
        in_live, in_def = p in L, p in D
        if in_live and in_def:
            status = "over" if L[p] != D[p] else "def"
        elif in_live:
            status = "ext"
        else:
            status = "unset"

        drift = None
        if T is not None:
            if is_engine_written(p):
                drift = "auto" if (L.get(p) != T.get(p)) else None
            elif in_live and p in T and L[p] != T[p]:
                drift = "changed"
            elif in_live and p not in T:
                drift = "new"
            elif not in_live and p in T:
                drift = "lost"

        rows.append({
            "path": p,
            "status": status,
            "drift": drift,
            "default": mask(p, D[p]) if in_def else None,
            "has_default": in_def,
            "live": mask(p, L[p]) if in_live else None,
            "in_live": in_live,
            "ours": any(p.startswith(o) for o in OURS),
        })
    return rows


def load_descriptions() -> tuple[list, dict, list]:
    """Exact descriptions + wildcard rules.

    A path with an asterisk (`agent.personalities.*`) describes a whole family of keys —
    otherwise one description would have to be copied into a dozen near-identical
    rows, and the generator would count them as undescribed.
    """
    if not descr_path() or not os.path.exists(descr_path()):
        return [], {}, []
    with open(descr_path(), encoding="utf-8") as fh:
        data = json.load(fh)
    sections = data.get("sections", [])
    index: dict = {}
    globs: list = []
    for sec in sections:
        for item in sec.get("keys", []):
            entry = {"text": item.get("text", ""), "flags": item.get("flags", []),
                     "section": sec["id"]}
            if "*" in item["path"]:
                globs.append((item["path"], entry))
            else:
                index[item["path"]] = entry
    return sections, index, globs


def describe(path: str, index: dict, globs: list) -> dict | None:
    """An exact description takes priority over a wildcard one."""
    if path in index:
        return index[path]
    for pattern, entry in globs:
        if fnmatch.fnmatchcase(path, pattern):
            return entry
    return None


FLAG_LABEL = {
    "redline": ("red line", "Do not touch without a separate conversation"),
    "alias": ("alias", "The engine stores this key under another name"),
    "dormant": ("dormant", "Not in effect: the parent key is off"),
    "temp-off": ("temporarily off", "Bring back when the reason is gone"),
    "nested": ("nested", "The meaning belongs to the parent key"),
    "ours": ("our extension", "The key is not in the engine schema"),
}


# ─────────────────────────── layout ───────────────────────────

def group_header_html(ns: str, members: list[dict], sec_id: str) -> str:
    """Group header: namespace + what makes the group notable.

    Computed from the group's actual members, not from a description — so it can
    never disagree with the table below it.
    """
    over = sum(1 for r in members if r["status"] == "over")
    drift = sum(1 for r in members if r["drift"] and r["drift"] != "auto")
    in_file = sum(1 for r in members if r["in_live"])
    meta = [f"{len(members)} " + (_("key") if len(members) == 1 else _("keys"))]
    if over:
        meta.append(f"{over} " + _("overridden"))
    if drift:
        meta.append(f"<b class=\"cfhd\">{drift} " + _("drifting") + "</b>")
    # the group key includes the section: one namespace lives in several sections
    # (part of `compression.*` is described, part is not), and without the section a
    # row from one section would light up the header of another
    return (
        f'<tr class="cfhead" data-g="{esc(sec_id + "|" + ns)}" data-sec="{esc(sec_id)}"'
        f' data-file="{"1" if in_file else "0"}">'
        f'<td colspan="4"><span class="cfhk">{esc(ns)}</span>'
        f'<span class="cfhm">{" · ".join(meta)}</span></td></tr>'
    )


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def row_html(r: dict, descr: dict | None, sec_id: str = "", grouped: bool = False) -> str:
    p = r["path"]
    label, cls = STATUS_LABEL[r["status"]]
    drift = r["drift"]

    pills = [f'<span class="cfp {cls}">{esc(_(label))}</span>']
    if drift and drift != "auto":
        pills.append(f'<span class="cfp drift" title="{esc(_(DRIFT_LABEL[drift]))}">{_("drift")}</span>')
    elif drift == "auto":
        pills.append(f'<span class="cfp auto" title="{esc(_("The engine writes this key itself"))}">{_("auto")}</span>')
    for flag in (descr or {}).get("flags", []):
        fl = FLAG_LABEL.get(flag)
        if fl:
            pills.append(f'<span class="cfp flag" title="{esc(_(fl[1]))}">{esc(_(fl[0]))}</span>')

    dflt = fmt(r["default"]) if r["has_default"] else "—"
    live = fmt(r["live"]) if r["in_live"] else "—"
    text = (descr or {}).get("text", "")

    def cell(raw: str, extra: str = "") -> str:
        short = raw if len(raw) <= VALUE_MAX else raw[:VALUE_MAX - 1] + "…"
        title = f' title="{esc(raw)}"' if short != raw else ""
        return f'<td class="num{extra}"{title}>{esc(short)}</td>'

    doc_cell = esc(text) if text else '<span class="cfno">—</span>'
    live_cls = " cflive" + (" cfdr" if drift and drift != "auto" else "")

    # Inside a group the namespace is already named by the header — the row keeps only
    # the last segment, the one that actually varies within the group. The full path is
    # not lost: it lives in data-k (used by the filter) and in the cell title.
    ns, _sep, leaf = p.rpartition(".")
    key_html = esc(leaf) if (grouped and ns) else esc(p)

    return (
        f'<tr class="cfrow{" cfsub" if grouped else ""}" data-k="{esc(p)}"'
        f' data-s="{esc(r["status"])}" data-g="{esc(sec_id + "|" + ns) if grouped else ""}"'
        f' data-sec="{esc(sec_id)}"'
        f' data-drift="{esc(drift or "")}" data-doc="{"1" if text else "0"}">'
        f'<td title="{esc(p)}"><code class="cfk">{key_html}</code>'
        f'<span class="cfpills">{"".join(pills)}</span></td>'
        + cell(dflt, " cfdim")
        + cell(live, live_cls)
        + f'<td class="cfd">{doc_cell}</td>'
        "</tr>"
    )


def render_group_rows(ordered: list[dict], index: dict, globs: list, sec_id: str) -> list[str]:
    """Lays out a section's rows by namespace groups, each with a header.

    Grouping FORCES group members to be adjacent (ordered by first appearance of the
    group and its keys in the curated order): otherwise one careless reorder in
    config-descriptions.json would produce two headers for the same group.
    Root keys and single-key groups get no header — that would be noise.
    """
    groups: dict = {}
    order: list = []
    for r in ordered:
        ns = r["path"].rpartition(".")[0]
        if ns not in groups:
            groups[ns] = []
            order.append(ns)
        groups[ns].append(r)

    out: list[str] = []
    for ns in order:
        members = groups[ns]
        grouped = bool(ns) and len(members) > 1
        if grouped:
            out.append(group_header_html(ns, members, sec_id))
        for r in members:
            out.append(row_html(r, describe(r["path"], index, globs), sec_id, grouped))
    return out


def section_html(title: str, hint: str, rows_html: list[str], sec_id: str = "") -> str:
    if not rows_html:
        return ""
    return (
        f'<div class="sec cfsec" data-sec="{esc(sec_id)}">'
        f'<div class="sec-h"><h2>{esc(title)}</h2>'
        f'<span class="ln"></span><span class="note">{esc(hint)}</span></div>'
        '<div class="tw"><table class="cftab"><thead><tr>'
        f'<th>{_("Key")}</th><th>{_("Default")}</th><th>{_("Live")}</th><th class="cfdh">{_("Description")}</th>'
        f'</tr></thead><tbody>{"".join(rows_html)}</tbody></table></div></div>'
    )


JS = """(function(){
  var q = document.getElementById("cfq");
  var empty = document.getElementById("cfempty");
  var count = document.getElementById("cfcount");
  var rows = [].slice.call(document.querySelectorAll("tr.cfrow"));
  var heads = [].slice.call(document.querySelectorAll("tr.cfhead"));
  var secs = [].slice.call(document.querySelectorAll(".cfsec"));
  var btns = [].slice.call(document.querySelectorAll(".cffb"));
  var mode = "file";
  function matches(r){
    if(mode === "over" && r.dataset.s !== "over") return false;
    if(mode === "drift" && !r.dataset.drift) return false;
    if(mode === "doc" && r.dataset.doc !== "1") return false;
    if(mode === "file" && r.dataset.s === "unset") return false;
    var needle = (q && q.value || "").trim().toLowerCase();
    if(!needle) return true;
    /* inside a group the row text holds only the last segment, so we also search
       the full path from data-k — otherwise the query "compression.threshold"
       would not find its own row */
    return (r.dataset.k + " " + r.textContent).toLowerCase().indexOf(needle) !== -1;
  }
  function apply(){
    var shown = 0, liveG = {}, liveS = {};
    for(var i = 0; i < rows.length; i++){
      var ok = matches(rows[i]);
      rows[i].style.display = ok ? "" : "none";
      if(ok){ shown++; liveG[rows[i].dataset.g] = 1; liveS[rows[i].dataset.sec] = 1; }
    }
    /* a group header lives exactly as long as its keys: count via data-g,
       not by walking the DOM — so the check survives any row reordering */
    for(var j = 0; j < heads.length; j++){
      heads[j].style.display = liveG[heads[j].dataset.g] ? "" : "none";
    }
    for(var s = 0; s < secs.length; s++){
      secs[s].style.display = liveS[secs[s].dataset.sec] ? "" : "none";
    }
    if(count) count.textContent = shown;
    if(empty) empty.style.display = shown ? "none" : "block";
  }
  btns.forEach(function(b){
    b.addEventListener("click", function(){
      mode = b.dataset.f;
      btns.forEach(function(x){ x.classList.toggle("on", x === b); });
      apply();
    });
  });
  if(q) q.addEventListener("input", apply);
  apply();
})();"""


def build_html(rows: list[dict], sections: list, index: dict, globs: list,
               truth_meta: str, has_truth: bool) -> tuple[str, int]:
    by_path = {r["path"]: r for r in rows}
    used: set = set()
    blocks: list[str] = []

    for sec in sections:
        ordered = []
        for item in sec.get("keys", []):
            pattern = item["path"]
            if "*" in pattern:
                targets = [r for r in rows if fnmatch.fnmatchcase(r["path"], pattern)]
            else:
                r = by_path.get(pattern)
                targets = [r] if r is not None else []
            for r in targets:
                if r["path"] in used:
                    continue
                used.add(r["path"])
                ordered.append(r)
        blocks.append(section_html(sec["title"], sec.get("hint", ""),
                                   render_group_rows(ordered, index, globs, sec["id"]),
                                   sec["id"]))

    # everything undescribed is grouped by the top-level path segment so the map
    # stays navigable even without prose
    rest = [r for r in rows if r["path"] not in used]
    blocks.append(section_html(
        _("Other keys · no description"),
        _("values are live, prose is added as needed"),
        render_group_rows(rest, index, globs, "rest"), "rest"))

    total = len(rows)
    over = sum(1 for r in rows if r["status"] == "over")
    ext = sum(1 for r in rows if r["status"] == "ext")
    unset = sum(1 for r in rows if r["status"] == "unset")
    drift = sum(1 for r in rows if r["drift"] and r["drift"] != "auto")
    documented = sum(1 for r in rows if (describe(r["path"], index, globs) or {}).get("text"))
    in_file = total - unset

    drift_cls = "no" if drift else "ok"
    drift_x = (_("die when the working copy is reset to the truth ref") if drift
               else (_("live config matches the truth ref") if has_truth else _("truth ref unavailable")))

    head = (
        f'<div class="vh"><div class="kick">{_("Configuration · three sources")}</div>'
        f'<h1>{_("Config")}</h1>'
        f'<p>{_("What the engine ships out of the box, what is really in effect on the server now, and <b>why it is set that way</b>. Values are recomputed on every dashboard run; descriptions are written by hand — so the prose talks about mechanics, not a snapshot.")}</p>'
        f'<div class="meta">{_("defaults")} — <b>DEFAULT_CONFIG</b> {_("from the running core")} · {_("effective")} — '
        f'<b>config.yaml</b> · {_("truth")} — <b>{esc(TRUTH_REF)}</b> ({esc(truth_meta)})</div></div>'
    )

    kpis = (
        '<div class="kpis prime">'
        f'<div class="kpi"><div class="l">{_("Keys in the map")}</div><div class="v">{total}</div>'
        f'<div class="x">{_("of them in our file")}: {in_file}</div></div>'
        f'<div class="kpi info"><div class="l">{_("Overridden")}</div><div class="v">{over}</div>'
        f'<div class="x">{_("value differs from the engine default")}</div></div>'
        f'<div class="kpi {drift_cls}"><div class="l">{_("Drift")}</div><div class="v">{drift}</div>'
        f'<div class="x">{esc(drift_x)}</div></div>'
        f'<div class="kpi ok"><div class="l">{_("Described")}</div><div class="v">{documented}</div>'
        f'<div class="x">{_("outside the engine schema")}: {ext} · {_("not set")}: {unset}</div></div>'
        '</div>'
    )

    controls = (
        '<div class="controls cfctl">'
        f'<input id="cfq" placeholder="{_("Search by key, value or description…")}">'
        f'<button class="fbtn cffb on" data-f="file">{_("in our file")}</button>'
        f'<button class="fbtn cffb" data-f="over">{_("overridden")}</button>'
        f'<button class="fbtn cffb" data-f="drift">{_("drift")}</button>'
        f'<button class="fbtn cffb" data-f="doc">{_("described")}</button>'
        f'<button class="fbtn cffb" data-f="all">{_("all engine keys")}</button>'
        f'<span class="cfcnt"><b id="cfcount">0</b> {_("rows")}</span>'
        '</div>'
        f'<div id="cfempty" class="cfempty" style="display:none">{_("Nothing found.")}</div>'
    )

    body = head + kpis + controls + "".join(blocks) + f"<script>{JS}</script>"
    return body, total


# ─────────────────────────── self-check ───────────────────────────

def selfcheck(page: str, expected_rows: int) -> list[str]:
    """Checks the CONTENT before writing. If it fails, no file is written at all."""
    problems: list[str] = []

    if "@@" in page:
        problems.append("unreplaced placeholders (@@) left in the output")

    # a grouped row also carries the cfsub class, so counting the exact
    # '<tr class="cfrow"' would only count rows outside groups
    got = len(re.findall(r'<tr class="cfrow[ "]', page))
    if got != expected_rows:
        problems.append(f"{got} rows in the markup, expected {expected_rows}")

    script = page[page.rfind("<script>") + len("<script>"): page.rfind("</script>")]
    # string literals and property accesses do not declare identifiers —
    # without this the check below flags `r.dataset.s` as "use of s before var s"
    bare = re.sub(r'"[^"]*"|\'[^\']*\'', '""', script)

    # ids the script refers to must exist in the markup (classic typo source)
    for ident in set(re.findall(r'getElementById\("([^"]+)"\)', script)):
        if f'id="{ident}"' not in page:
            problems.append(f'getElementById("{ident}") without id="{ident}" in the markup')

    # use-before-declaration: valid syntax, node --check stays silent,
    # but the browser throws ReferenceError on the first render line
    for m in re.finditer(r"\b(?:var|let|const)\s+([A-Za-z_$][\w$]*)", bare):
        name = m.group(1)
        first = re.search(r"(?<![\w.$])" + re.escape(name) + r"\b", bare)
        if first and first.start() < m.start():
            problems.append(f"identifier {name} is used before its declaration")

    if SECRET_VALUE_RE.search(page):
        problems.append("a value that looks like a live secret is on the page")

    # the only check that catches arbitrary runtime errors
    if os.path.exists(NODE):
        # The shim must expose the same dataset fields the script reads: otherwise
        # the check fails on its own stub rather than on a real error.
        shim = """
        function mkEl(){return {style:{},dataset:{s:"over",drift:"",doc:"1",
          k:"a.b",g:"a",sec:"x",f:"1"},classList:{
          add:function(){},remove:function(){},toggle:function(){},contains:function(){return false}},
          textContent:"",value:"",addEventListener:function(){},
          querySelector:function(){return null},querySelectorAll:function(){return []}};}
        var document={getElementById:function(){return mkEl()},
          querySelectorAll:function(){return [mkEl(),mkEl(),mkEl()]},
          addEventListener:function(){}};
        """
        try:
            r = subprocess.run([NODE, "-e", shim + script], capture_output=True,
                               text=True, timeout=20)
            if r.returncode != 0:
                # take the line with the error itself, not the tail of the trace: node's
                # last line is its version string, which says nothing about the cause
                lines = [ln.strip() for ln in r.stderr.splitlines() if ln.strip()]
                why = next((ln for ln in lines if "Error" in ln), lines[0] if lines else "?")
                problems.append(f"the script fails under the DOM shim: {why}")
        except Exception as e:  # noqa: BLE001
            problems.append(f"could not run the script under node: {e}")

    return problems


def uncovered_keys(rows: list[dict], index: dict, globs: list) -> list[str]:
    return [r["path"] for r in rows
            if r["in_live"] and not (describe(r["path"], index, globs) or {}).get("text")]


def coverage_report(rows: list[dict], index: dict, globs: list) -> str:
    """Keys present in the live config but missing from the descriptions map.

    Empty list = descriptions are up to date. Non-empty = a reminder to write them.
    Without this the map silently decays.
    """
    known = {r["path"] for r in rows}
    uncovered = uncovered_keys(rows, index, globs)
    stale = [p for p in index if p not in known]
    stale += [p for p, _e in globs
              if not any(fnmatch.fnmatchcase(k, p) for k in known)]
    out = [f"described exactly: {len(index)} · patterns: {len(globs)} · "
           f"undescribed in the live config: {len(uncovered)}"]
    if stale:
        out.append("DESCRIBED, BUT THE KEY IS GONE (clean up): " + ", ".join(sorted(stale)))
    out.extend("  undescribed: " + p for p in uncovered)
    return "\n".join(out)


def main() -> None:
    from .i18n import set_lang
    set_lang(os.environ.get("HERMES_DASHBOARD_LANG", _cfg.default_lang))
    defaults = load_defaults()
    live = load_live()
    truth, truth_meta = load_truth()
    rows = build_rows(defaults, live, truth)
    sections, index, globs = load_descriptions()

    if "--report" in sys.argv:
        print(coverage_report(rows, index, globs))
        return

    page, total = build_html(rows, sections, index, globs, truth_meta, truth is not None)

    problems = selfcheck(page, total)
    if problems:
        sys.stderr.write("gen_config: SELF-CHECK FAILED, page not updated:\n")
        for p in problems:
            sys.stderr.write(f"  ✗ {p}\n")
        sys.exit(1)

    # coverage goes to stderr: an empty tail means the descriptions are up to date
    uncovered = uncovered_keys(rows, index, globs)
    sys.stderr.write(f"gen_config: {total} rows, {len(uncovered)} undescribed in the live config\n")

    try:
        os.makedirs(os.path.dirname(cache_path()), exist_ok=True)
        # Unique temp name: two builds racing (cron plus a manual rebuild) would
        # otherwise share `<cache>.tmp` and one would publish the other's bytes.
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(cache_path()),
                                   prefix="." + os.path.basename(cache_path()) + ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(page)
        os.replace(tmp, cache_path())  # atomic: a half-written file never goes out
    except Exception as e:  # noqa: BLE001 — the cache is optional, the page is still returned
        sys.stderr.write(f"gen_config: cache not written: {e}\n")

    sys.stdout.write(page)


if __name__ == "__main__":
    main()
