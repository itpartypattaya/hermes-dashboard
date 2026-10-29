"""Memory map view: what the agent remembers, for a person to read.

The data is not read here. The holographic memory plugin ships an exporter
(`memory_map.py`, docs/memory-map.md in that repo) that turns ONE store into a
JSON snapshot and applies every privacy rule on the way: a forgotten fact is
its number and date, a corrected version has no text, a text the provider's
safety screen keeps out of answers is not exported, other scopes are never
opened. This module runs that exporter in a separate process, read-only, with a
timeout, and draws what it printed — nothing is added from the store, so the
dashboard cannot disagree with the plugin about what may be shown.

Off by default (`views.memory_map`). When it is on and the export fails for any
reason the tab says so instead of showing numbers, and the build log gets a
`[section] memory-map failed: …` line; the rest of the dashboard is unaffected.

Every string that comes from the snapshot is memory content written from chat
messages, so it is escaped like any other untrusted text.
"""
from __future__ import annotations

import json
import math
import os
import re
import signal
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

from .common import esc
from .config import Config, child_environment
from .i18n import _

ASSETS = Path(__file__).parent.parent / "assets"
SUPPORTED_SCHEMAS = (1,)
# The keys the view reads, and the shape each must have. A snapshot without them
# is not a smaller map, it is a different program's output.
REQUIRED = {"SNAP": dict, "ENT": list, "FACTS": list, "CONFLICTS": list, "PENDING": list,
            "TOMBS": list, "MERGES": list, "HIDDEN": list, "FEED": list}
TASKS_ON_OVERVIEW = 4


class MapUnavailable(Exception):
    """The export did not give a usable snapshot. `code` picks the sentence the tab shows."""

    def __init__(self, code: str, detail: str = "", value=None):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.value = code, value


# ── collecting ────────────────────────────────────────────────────────

def _python(cfg: Config) -> str:
    """memory_map.python, else the agent's venv (the plugin's own deps), else ours."""
    explicit = str(cfg.get("memory_map.python", "") or "")
    if explicit:
        return str(cfg.path_in_home(explicit))
    venv = cfg.venv_python
    if venv and Path(venv).is_file():
        return venv
    return sys.executable


def _tz(cfg: Config) -> str:
    """The snapshot's zone as a fixed offset: an IANA name needs tzdata, an offset never does."""
    off = cfg.number("timezone.offset_hours", 0, int)
    return "%s%02d:00" % ("-" if off < 0 else "+", abs(off))


def command(cfg: Config) -> list[str]:
    plugin = str(cfg.get("memory_map.plugin_dir", "") or "")
    if not plugin:
        raise MapUnavailable("not_configured")
    script = cfg.path_in_home(plugin) / "memory_map.py"
    if not script.is_file():
        raise MapUnavailable("no_plugin", str(script))
    store = cfg.path_in_home(str(cfg.get("memory_map.store", "memory_store.db") or "memory_store.db"))
    if not store.is_file():
        raise MapUnavailable("no_store", str(store))
    cmd = [_python(cfg), str(script), "--store", str(store), "--json", "--tz", _tz(cfg)]
    settings = Path(cfg.home) / "config.yaml"
    if settings.is_file():  # the search settings that order a contradiction's two sides
        cmd += ["--config", str(settings)]
    if cfg.get("memory_map.dreaming", True):
        cmd += ["--hermes-home", str(cfg.home)]
    return cmd


def _run(cmd: list[str], env: dict, timeout: float) -> tuple[int, bytes, bytes]:
    """(returncode, stdout, stderr). On POSIX the exporter gets a process group of its own, so a timeout stops
    what a wrapper in memory_map.python (uv run, a shell script) started too, not only the wrapper."""
    posix = os.name == "posix"
    with subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          start_new_session=posix) as p:
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            if posix:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    p.kill()
            else:
                p.kill()
            p.communicate()
            raise
    return p.returncode, out, err


_ERROR_NAME = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt))\b")


def _stderr_kind(err: bytes) -> str:
    """What the build log may say about a failed export: the error's class, never its message. A message can
    hold a stored text (a ValueError raised on a value read from the store), and the log is not behind the
    password."""
    lines = [ln.strip() for ln in err.decode("utf-8", "replace").splitlines() if ln.strip()]
    m = _ERROR_NAME.match(lines[-1]) if lines else None
    if m:
        return m.group(1)
    if lines and lines[-1].startswith("memory_map:"):
        return "memory_map error"
    return f"stderr {len(err)} bytes"


def collect(cfg: Config) -> dict:
    """Run the exporter and return its snapshot, or raise MapUnavailable."""
    timeout = cfg.number("memory_map.timeout_s", 60, float)
    cmd = command(cfg)
    try:
        # The exporter is the plugin's code: it gets the agent home, not the dashboard's own credentials.
        rc, out, err = _run(cmd, child_environment(cfg.home, credentials=False), timeout)
    except subprocess.TimeoutExpired:
        raise MapUnavailable("timeout", f"after {timeout:g} s", timeout) from None
    except OSError as e:
        raise MapUnavailable("not_run", type(e).__name__) from None
    if rc != 0:
        raise MapUnavailable("exit", f"rc={rc} {_stderr_kind(err)}", rc)
    try:
        data = json.loads(out.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise MapUnavailable("not_json", f"{len(out)} bytes") from None
    if not isinstance(data, dict) or data.get("schema_version") not in SUPPORTED_SCHEMAS:
        got = data.get("schema_version") if isinstance(data, dict) else type(data).__name__
        raise MapUnavailable("schema", f"schema_version {got!r}")
    for key, kind in REQUIRED.items():
        if not isinstance(data.get(key), kind):
            raise MapUnavailable("schema", f"{key} is not a {kind.__name__}")
    return data


def load(cfg: Config) -> tuple[dict | None, MapUnavailable | None]:
    """(snapshot, None) or (None, why). The failure line has the shape build._safe prints."""
    try:
        return collect(cfg), None
    except MapUnavailable as e:
        print("[section] memory-map failed: %s" % (e,), file=sys.stderr)
        return None, e


# ── small formatting helpers ──────────────────────────────────────────

def pl(n: int, forms: str) -> str:
    """Pick the plural form of an already translated "one|other" (en) or "one|few|many" (ru) key."""
    parts = forms.split("|")
    if len(parts) == 3:
        i = 0 if n % 10 == 1 and n % 100 != 11 else 1 if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else 2
    elif len(parts) == 2:
        i = 0 if n == 1 else 1
    else:
        i = 0
    return parts[i].format(n=n)


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _day(v) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


class _Ctx:
    """The snapshot with its lookups, built once per language pass."""

    def __init__(self, cfg: Config, data: dict):
        self.cfg, self.data = cfg, data
        self.snap = data["SNAP"]
        self.facts = {f["id"]: f for f in data["FACTS"] if isinstance(f, dict) and _int(f.get("id")) is not None}
        self.ents = {e["id"]: e for e in data["ENT"] if isinstance(e, dict) and _int(e.get("id")) is not None}
        self.made = None
        try:
            self.made = datetime.fromisoformat(str(self.snap.get("made")))
        except (TypeError, ValueError):
            pass
        self.year = self.made.year if self.made else None
        self.conflicts = [c for c in data["CONFLICTS"] if isinstance(c, dict)]
        self.pending = [p for p in data["PENDING"] if isinstance(p, dict)]
        self.feed = [x for x in data["FEED"] if isinstance(x, dict)]
        self.new_week = {x.get("f") for x in self.feed if x.get("k") == "new"}

    # dates: day.month, with the year only when it is not the snapshot's
    def d(self, v) -> str:
        day = _day(v)
        if day is None:
            return "—"
        return day.strftime("%d.%m") if day.year == self.year else day.strftime("%d.%m.%Y")

    def in_conflict(self, fid) -> list[dict]:
        return [c for c in self.conflicts if fid in (c.get("a"), c.get("b"))]

    def merges_of(self, fid) -> list[dict]:
        return [p for p in self.pending if p.get("kind") == "merge" and fid in (p.get("keep"), p.get("copy"))]

    def needs_attention(self, f) -> bool:
        return f.get("status") == "current" and bool(
            f.get("weak") or self.in_conflict(f["id"]) or self.merges_of(f["id"]))


# ── labels (English keys; the locale has the rest) ────────────────────

def origin_label(o) -> tuple[str, str]:
    return {
        "said": ("o-said", _("said by a person")),
        "confirmed": ("o-conf", _("inference, confirmed")),
        "inferred": ("o-inf", _("inference, not confirmed")),
    }.get(o, ("o-unk", _("origin unknown")))


def trust_label(level) -> str:
    return {"weak": _("weak trust"), "normal": _("normal trust"), "strong": _("strong trust")}.get(level, _("trust not set"))


def via_label(v) -> str:
    return {"tool": _("written in a conversation"), "auto_extract": _("extracted automatically from a message"),
            "mirror": _("copied from long-term memory"),
            "legacy": _("old record, written before sources were recorded")}.get(v, esc(v or "—"))


def chat_label(c) -> str:
    if not c:
        return _("chat not recorded")
    return {"dm": _("private chat"), "private": _("private chat"), "group": _("group chat"),
            "supergroup": _("group chat"), "channel": _("channel"), "thread": _("thread")}.get(c, esc(c))


def weak_label(code, f) -> str:
    if code == "near_duplicate" and _int(f.get("dup_of")) is not None:
        return _("almost repeats fact №{n}").format(n=f["dup_of"])
    return {"low_trust": _("low trust: below 0.3, search no longer finds it"),
            "never_recalled": _("never came up in 60+ days"),
            "unconfirmed_inference": _("an inference nobody confirmed in 60+ days"),
            "near_duplicate": _("almost repeats another fact"),
            "no_source": _("source not recorded")}.get(code, esc(code))


def version_status(s) -> str:
    return {"current": _("in force"), "changed": _("changed"), "ended": _("closed"),
            "corrected": _("a mistake, corrected"), "merged": _("merged")}.get(s, esc(s))


def type_label(c, e) -> str:
    """Why a name is listed as a person or a topic, or that nothing says which (HTML)."""
    t, by = e.get("type"), e.get("type_by")
    if t == "person":
        if by == "speaker":
            return esc(_("person: someone of this name wrote in the chat"))
        out = esc(_("person: a word for a relative or a role stands beside the name"))
        if _int(e.get("type_fact")) is not None:
            out += " — " + fref(e["type_fact"])
        return out
    if t == "topic":
        return esc({"digit": _("topic: the name has a digit"),
                    "identifier": _("topic: the name is written like an identifier"),
                    "mixed_case": _("topic: the name is written like a brand"),
                    "acronym": _("topic: the name is an abbreviation")}.get(by, _("topic")))
    return esc(_("not determined: nothing in memory says whether this is a person"))


def screen_label(why) -> str:
    return {"credential": _("looks like a password or a key"), "instruction": _("an instruction to the assistant"),
            "markup": _("hidden markup")}.get(why, esc(why))


def alert_label(kind) -> str:
    return {"messages_unavailable": _("messages were unavailable — «nobody mentioned it» means nothing that night"),
            "memory_loss": _("long-term memory shrank unexpectedly"),
            "probe_missing": _("the pre-check did not run"),
            "pending_writes": _("writes are waiting for approval"),
            "duplicates_budget": _("more duplicates than one question a night can cover")}.get(kind, esc(kind))


# ── pieces ────────────────────────────────────────────────────────────

def fref(fid) -> str:
    """A link to a fact by its number; a value that is not one shows as an unknown number, never as its text."""
    n = _int(fid)
    return f'<a class="mm-fid" href="#mmap/f{n}">№{n}</a>' if n is not None else "№?"


def origin_chip(o) -> str:
    cls, label = origin_label(o)
    return f'<span class="mm-chip {cls}">{esc(label)}</span>'


def trust_chip(f, with_number: bool = False) -> str:
    level = f.get("trust_level")
    n = {"weak": 1, "normal": 2, "strong": 3}.get(level, 0)
    num = f.get("trust")
    title = _("trust {t} of 1").format(t=f"{num:.2f}") if isinstance(num, (int, float)) else ""
    extra = f' <span class="mm-num">({num:.2f})</span>' if with_number and isinstance(num, (int, float)) else ""
    return (f'<span class="mm-trust t{n}" title="{esc(title)}"><i></i><i></i><i></i>'
            f'{esc(trust_label(level))}{extra}</span>')


def ent_chip(c: _Ctx, eid) -> str:
    e = c.ents.get(eid)
    if not e:
        return ""
    return f'<a class="mm-chip mm-ent" href="#mmap/e{int(eid)}">{esc(e.get("name"))}</a>'


def fact_flags(c: _Ctx, f: dict) -> str:
    """The state lines under a fact: closed, contradicted, weak, proposed for a merge."""
    fid = f["id"]
    weak = f.get("weak") or []
    conflicts = c.in_conflict(fid)
    merges = c.merges_of(fid)
    flags = []
    if f.get("status") == "ended":
        flags.append(f'<div class="mm-flag">{_("No longer in force")}'
                     + (f' · {_("until {d}").format(d=c.d(f.get("to")))}' if f.get("to") else "") + '</div>')
    if conflicts:
        others = ", ".join(fref(x.get("b") if x.get("a") == fid else x.get("a")) for x in conflicts)
        flags.append(f'<div class="mm-flag bad">{_("Disagrees with")} {others} — '
                     f'<a href="#mmap/conflicts">{_("see the conflict")}</a></div>')
    if weak:
        flags.append(f'<div class="mm-flag">{_("On a weak basis")}: '
                     + " · ".join(weak_label(w, f) for w in weak) + '</div>')
    if merges:
        flags.append(f'<div class="mm-flag">{_("Proposed to merge")} ('
                     + ", ".join(esc(m.get("id")) for m in merges)
                     + f') — <a href="#mmap/pending">{_("waits for a decision")}</a></div>')
    return "".join(flags)


def fact_card(c: _Ctx, f: dict, hide_ent=None) -> str:
    fid = f["id"]
    cls = "mm-fact" + (" weak" if f.get("weak") else "") + (" ended" if f.get("status") == "ended" else "")
    meta = [origin_chip(f.get("origin")), trust_chip(f),
            f'<span class="mm-m">{_("recorded {d}").format(d=c.d(f.get("rec")))}'
            + (f' · {_("from the words of {who}").format(who=esc(f.get("by")))}' if f.get("by") else "") + '</span>']
    conf = [x for x in f.get("conf") or [] if isinstance(x, dict)]
    if conf:
        meta.append(f'<span class="mm-m">{pl(len(conf), _("confirmed {n} time|confirmed {n} times"))}</span>')
    nver = len(f.get("versions") or [])
    if nver > 1:
        meta.append(f'<span class="mm-m">{pl(nver, _("{n} version|{n} versions"))}</span>')
    if f.get("cat"):
        meta.append(f'<span class="mm-chip">{esc(f.get("cat"))}</span>')
    meta += [ent_chip(c, e) for e in f.get("ents") or [] if e != hide_ent]
    return (f'<article class="card {cls}"><p class="mm-text">{fref(fid)} '
            f'<a href="#mmap/f{int(fid)}">{esc(f.get("text"))}</a></p>'
            f'<div class="mm-meta">{"".join(meta)}</div>{fact_flags(c, f)}</article>')


def sec(title: str, note: str = "", body: str = "") -> str:
    n = f'<span class="note">{note}</span>' if note else ""
    return (f'<div class="sec"><div class="sec-h"><h2>{title}</h2><span class="ln"></span>{n}</div>'
            f'{body}</div>')


def hint(html: str) -> str:
    return f'<div class="mm-hint">{html}</div>'


# ── pending ("waits for your decision") ───────────────────────────────

def kind_tag(p) -> str:
    k = p.get("kind")
    cls, label = {"conflict": ("p-no", _("contradiction")), "merge": ("p-part", _("duplicate")),
                  "memory_conflict": ("p-no", _("fact vs long-term memory")),
                  "relevant": ("p-avail", _("still relevant?")),
                  "queue": ("p-avail", _("waiting for approval"))}.get(k, ("tg id", str(k or "?")))
    # D-NN is what the person answers («yes D-12»); the export's own counters (C1, M-1, R-7) no command takes
    pid = p.get("id") if k == "merge" else None
    return f'<span class="pill {cls}">{esc(label)}{" " + esc(pid) if pid else ""}</span>'


def task_title(c: _Ctx, p) -> str:
    k = p.get("kind")
    if k == "conflict":
        return _("Facts №{a} and №{b} say different things — which is right?").format(a=esc(p.get("a")), b=esc(p.get("b")))
    if k == "merge":
        return _("Facts №{a} and №{b} say the same — merge them?").format(a=esc(p.get("keep")), b=esc(p.get("copy")))
    if k == "memory_conflict":
        return _("Fact №{n} disagrees with an entry of long-term memory").format(n=esc(p.get("fact")))
    if k == "relevant":
        return _("Is this long-term memory entry still relevant?")
    if k == "queue":
        return pl(_int(p.get("writes")) or 0, _("{n} write to long-term memory waits for approval|"
                                                "{n} writes to long-term memory wait for approval"))
    return esc(k)


def task_when(c: _Ctx, p) -> str:
    bits = []
    if p.get("created"):
        bits.append(_("created {d}").format(d=c.d(p["created"])))
    if p.get("kind") == "queue" and p.get("oldest"):
        bits.append(_("oldest from {d}").format(d=c.d(p["oldest"])))
    if p.get("kind") == "conflict" and not p.get("asked"):
        # found by the contradiction check at export time, not a question the agent asked
        bits.append(_("found when the snapshot was made"))
    else:
        bits.append(_("asked {d}").format(d=c.d(p["asked"])) if p.get("asked") else _("not asked yet"))
    return " · ".join(bits)


def task_row(c: _Ctx, p) -> str:
    return (f'<a class="mm-task" href="#mmap/pending"><span>{kind_tag(p)}</span>'
            f'<span class="mm-task-w">{task_title(c, p)}</span>'
            f'<span class="mm-m">{task_when(c, p)}</span></a>')


def side(c: _Ctx, fid, label: str) -> str:
    f = c.facts.get(fid)
    if not f:
        return f'<div class="mm-side"><div class="mm-lbl">{label}</div><p>№{esc(fid)}</p></div>'
    conf = len(f.get("conf") or [])
    return (f'<div class="mm-side"><div class="mm-lbl">{label}</div>'
            f'<p>{fref(fid)} {esc(f.get("text"))}</p><div class="mm-meta">{origin_chip(f.get("origin"))}'
            f'{trust_chip(f)}<span class="mm-m">{_("recorded {d}").format(d=c.d(f.get("rec")))}</span>'
            + (f'<span class="mm-m">{pl(conf, _("confirmed {n} time|confirmed {n} times"))}</span>' if conf else "")
            + '</div></div>')


def pending_card(c: _Ctx, p) -> str:
    k = p.get("kind")
    body, change, answer = "", "", ""
    if k == "conflict":
        a, b = p.get("a"), p.get("b")
        body = (f'<p class="mm-small">{fref(a)} ↔ {fref(b)} — '
                f'<a href="#mmap/conflicts">{_("both sides side by side")}</a></p>')
        change = _("After your answer one of the two changes; its old text stays in its history.")
    elif k == "merge":
        body = (f'<div class="mm-pair">{side(c, p.get("keep"), _("Stays"))}'
                f'{side(c, p.get("copy"), _("Goes into its history"))}</div>')
        if p.get("covered"):
            body += f'<p class="mm-small">{_("Every meaningful word of the copy is already in the fact that stays.")}</p>'
        elif p.get("extra"):
            body += (f'<p class="mm-small">{_("Only in the copy")}: '
                     + ", ".join(esc(w) for w in p["extra"] if isinstance(w, str)) + '</p>')
        if p.get("current") is False:
            body += f'<div class="mm-flag">{_("One of the two changed since the question was made: it will be asked again.")}</div>'
        change = _("The copy stops showing on its own; its text and source stay in the history of the fact that stays.")
        answer = _("Answer in the chat: «yes {id}» — merge · «{id}: these are different» — not asked again.").format(
            id=esc(p.get("id")))
    elif k == "memory_conflict":
        body = (f'<div class="mm-pair">{side(c, p.get("fact"), _("Fact"))}'
                f'<div class="mm-side"><div class="mm-lbl">{_("Long-term memory")}</div>'
                f'<p>{esc(p.get("entry"))}</p></div></div>')
        nums = [(_("numbers in the fact"), p.get("fact_numbers")), (_("numbers in memory"), p.get("memory_numbers")),
                (_("negations in the fact"), p.get("fact_negations")),
                (_("negations in memory"), p.get("memory_negations"))]
        shown = [f'{lbl}: {", ".join(esc(x) for x in v)}' for lbl, v in nums if isinstance(v, list) and v]
        if shown:
            body += f'<p class="mm-small">{" · ".join(shown)}</p>'
        change = _("After your answer either the fact or the memory entry is corrected.")
    elif k == "relevant":
        days = p.get("last_mention_days")
        body = f'<p class="mm-quote">{esc(p.get("text"))}</p>'
        if isinstance(days, (int, float)):
            body += f'<p class="mm-small">{pl(int(days), _("not mentioned for {n} day|not mentioned for {n} days"))}</p>'
        change = _("«Yes» — the entry rests for 90 days. «No» — it is removed from long-term memory; facts are not touched.")
    elif k == "queue":
        if _int(p.get("unreadable")):
            body = f'<p class="mm-small">{pl(p["unreadable"], _("{n} record could not be read|{n} records could not be read"))}</p>'
        change = _("Until you approve them the agent does not remember them. Their text is not shown here: it is not memory yet.")
        answer = _("In the chat: /memory pending — review and approve.")
    return (f'<div class="card mm-pend"><div class="mm-row">{kind_tag(p)}<span class="mm-m">{task_when(c, p)}</span></div>'
            f'<div class="mm-h">{task_title(c, p)}</div>{body}'
            + (f'<p class="mm-small"><b>{_("What changes")}:</b> {change}</p>' if change else "")
            + (hint(answer) if answer else "") + '</div>')


# ── panels ────────────────────────────────────────────────────────────

def ent_stats(c: _Ctx, eid) -> dict:
    fs = [f for f in c.facts.values() if eid in (f.get("ents") or [])]
    cur = [f for f in fs if f.get("status") == "current"]
    conf = [x.get("d") for f in fs for x in f.get("conf") or [] if isinstance(x, dict) and x.get("d")]
    return {"fs": fs, "cur": cur, "att": [f for f in cur if c.needs_attention(f)],
            "fresh": [f for f in cur if f["id"] in c.new_week],
            "last_rec": max((f.get("rec") or "" for f in fs), default=""), "last_conf": max(conf, default="")}


def ent_card(c: _Ctx, e) -> str:
    s = ent_stats(c, e["id"])
    bits = [f'<span>{pl(len(s["cur"]), _("{n} fact|{n} facts"))}</span>']
    if s["fresh"]:
        bits.append(f'<span>{_("+{n} this week").format(n=len(s["fresh"]))}</span>')
    if s["att"]:
        bits.append(f'<span class="att">{pl(len(s["att"]), _("{n} needs attention|{n} need attention"))}</span>')
    prov = (f' <span class="mm-chip o-unk" title="{esc(_("Guessed from the text of one fact; nobody confirmed it"))}">'
            f'{_("name not confirmed")}</span>' if e.get("standing") == "provisional" else "")
    aliases = [a for a in (e.get("aliases") or []) if isinstance(a, str)]
    sub = f'<div class="mm-sub">{_("also")}: {esc(", ".join(aliases))}</div>' if aliases else ""
    return (f'<a class="card mm-ec" href="#mmap/e{int(e["id"])}"><div class="mm-name">{esc(e.get("name"))}{prov}</div>'
            f'{sub}<div class="mm-cnt">{"".join(bits)}</div></a>')


def _people_topics(c: _Ctx) -> tuple[list, list, list]:
    """(people, topics, not determined), as the export typed them; a name it could not type is not a person."""
    order = sorted(c.ents.values(), key=lambda e: (-(_int(e.get("facts")) or 0), str(e.get("name"))))
    people = [e for e in order if e.get("type") == "person"]
    topics = [e for e in order if e.get("type") == "topic"]
    return people, topics, [e for e in order if e.get("type") not in ("person", "topic")]


def _ent_grids(c: _Ctx) -> str:
    people, topics, untyped = _people_topics(c)
    grid = lambda es: '<div class="mm-grid">' + "".join(ent_card(c, e) for e in es) + '</div>'  # noqa: E731
    out = ""
    if people:
        out += sec(_("People"), pl(len(people), _("{n} name|{n} names")), grid(people))
    if topics:
        out += sec(_("Topics and projects"), pl(len(topics), _("{n} name|{n} names")), grid(topics))
    if untyped:
        out += sec(_("Not determined"), _("memory does not say whether these are people"), grid(untyped))
    return out


def _counts(c: _Ctx) -> dict:
    """What each overview tile counts. The tab a tile leads to shows the same number on its button: names in
    question are not facts, so they are said apart, never added to the weak facts."""
    weak = [f for f in c.facts.values() if f.get("weak") and f.get("status") == "current"]
    return {"pending": len(c.pending), "conflicts": len(c.conflicts), "weak": len(weak),
            "new": len(c.new_week), "forgotten": len(c.data["TOMBS"]),
            "provisional": sum(1 for e in c.ents.values() if e.get("standing") == "provisional")}


def panel_home(c: _Ctx, n: dict) -> str:
    cur = sum(1 for f in c.facts.values() if f.get("status") == "current")
    tiles = [("pending", n["pending"], pl(n["pending"], _("waits for your decision|wait for your decision")), "warn" if n["pending"] else "ok"),
             ("conflicts", n["conflicts"], pl(n["conflicts"], _("contradiction|contradictions")), "no" if n["conflicts"] else "ok"),
             ("weak", n["weak"], pl(n["weak"], _("fact on a weak basis|facts on a weak basis")), ""),
             ("week", n["new"], pl(n["new"], _("new fact in 7 days|new facts in 7 days")), "info"),
             ("removed", n["forgotten"], pl(n["forgotten"], _("forgotten on request|forgotten on request")), "")]
    extra = {"weak": pl(n["provisional"], _("and {n} name in question|and {n} names in question"))
             if n["provisional"] else ""}
    kpis = "".join(f'<a class="kpi mm-kpi {cls}" href="#mmap/{key}"><div class="v">{num}</div>'
                   f'<div class="x">{esc(words)}</div>'
                   + (f'<div class="x mm-kpi-also">{esc(extra[key])}</div>' if extra.get(key) else "")
                   + '</a>' for key, num, words, cls in tiles)
    lead = (pl(cur, _("{n} current fact|{n} current facts")) + " · "
            + pl(len(c.ents), _("{n} name or topic|{n} names and topics")))
    tasks = c.pending[:TASKS_ON_OVERVIEW]
    task_html = ('<div class="card mm-tasks">' + "".join(task_row(c, p) for p in tasks)
                 + (f'<a class="mm-more" href="#mmap/pending">{_("All {n} →").format(n=len(c.pending))}</a>'
                    if len(c.pending) > TASKS_ON_OVERVIEW else "") + '</div>') if tasks else \
        f'<div class="card mm-small">{_("Nothing waits for a decision.")}</div>'
    return (f'<p class="mm-lead">{lead}</p><div class="kpis mm-kpis">{kpis}</div>'
            + sec(_("Waits for your decision"), "", task_html) + _ent_grids(c)
            + f'<p class="mm-foot">{_("Read-only. The map is rebuilt together with the dashboard; something forgotten disappears from it on the next rebuild.")} '
              f'<a href="#mmap/about">{_("About the snapshot")}</a></p>')


def panel_ents(c: _Ctx) -> str:
    loose = [f for f in c.facts.values() if not f.get("ents") and f.get("status") == "current"]
    out = _ent_grids(c)
    if loose:
        out += sec(_("Not linked to any name"), pl(len(loose), _("{n} fact|{n} facts")), _by_category(c, loose))
    return out or f'<div class="card mm-small">{_("No names in this store yet.")}</div>'


def _by_category(c: _Ctx, facts: list, hide_ent=None) -> str:
    groups: dict[str, list] = {}
    for f in facts:
        groups.setdefault(str(f.get("cat") or _("no category")), []).append(f)
    return "".join(f'<h3 class="mm-cat">{esc(cat)}</h3>' + "".join(fact_card(c, f, hide_ent) for f in fs)
                   for cat, fs in sorted(groups.items()))


def panel_entity(c: _Ctx, e) -> str:
    eid = e["id"]
    s = ent_stats(c, eid)
    calm = [f for f in s["cur"] if f not in s["att"]]
    past = [f for f in s["fs"] if f.get("status") != "current"]
    rel: dict[int, int] = {}
    for f in s["cur"]:
        for o in f.get("ents") or []:
            if o != eid and o in c.ents:
                rel[o] = rel.get(o, 0) + 1
    aliases = [a for a in (e.get("aliases") or []) + (e.get("text_aliases") or []) if isinstance(a, str)]
    standing = (_("name guessed from one fact on {d}, nobody confirmed it").format(d=c.d(e.get("since")))
                if e.get("standing") == "provisional" else _("name confirmed"))
    kv = (f'<dl class="mm-kv"><dt>{_("Last record")}</dt><dd>{c.d(s["last_rec"]) if s["last_rec"] else "—"}</dd>'
          f'<dt>{_("Last confirmation by a person")}</dt><dd>{c.d(s["last_conf"]) if s["last_conf"] else _("none")}</dd>'
          f'<dt>{_("Needs attention")}</dt><dd>{pl(len(s["att"]), _("{n} fact|{n} facts")) if s["att"] else _("nothing")}</dd></dl>')
    out = (f'<a class="mm-back" href="#mmap/ents">← {_("People and topics")}</a>'
           f'<h2 class="mm-title">{esc(e.get("name"))}</h2>'
           f'<p class="mm-small mm-type">{type_label(c, e)}</p>'
           f'<div class="mm-meta"><span class="mm-chip {"o-unk" if e.get("standing") == "provisional" else ""}">{esc(standing)}</span>'
           + (f'<span class="mm-m">{_("also")}: {esc(", ".join(aliases))}</span>' if aliases else "")
           + f'<span class="mm-m">{pl(len(s["cur"]), _("{n} fact|{n} facts"))}</span></div>' + kv)
    if s["att"]:
        out += sec(_("Needs attention"), "", "".join(fact_card(c, f, eid) for f in s["att"]))
    out += sec(_("What the agent knows"), "", _by_category(c, calm, eid) if calm
               else f'<p class="mm-small">{_("Nothing else — everything is above.")}</p>')
    if rel:
        rows = "".join(f'<div class="mm-rel">{ent_chip(c, o)}<span class="mm-m">'
                       f'{pl(k, _("{n} shared fact|{n} shared facts"))}</span></div>'
                       for o, k in sorted(rel.items(), key=lambda x: (-x[1], x[0])))
        out += sec(_("Connections"), _("a connection is a shared fact, not a relationship"),
                   f'<div class="card mm-eg">{ego_svg(c, e, rel)}<div class="mm-eg-list">{rows}</div></div>')
    if past:
        out += sec(_("Before"), "", "".join(fact_card(c, f, eid) for f in past))
    if c.data["TOMBS"]:
        out += (f'<p class="mm-foot">{_("Forgotten facts are not shown under anyone’s name: after «forget» their text is gone everywhere, here too.")} '
                f'<a href="#mmap/removed">{_("Removed and closed")}</a></p>')
    return out


EGO_MAX = 8


def _short(name: str, n: int = 14) -> str:
    return name if len(name) <= n else name[:n - 1].rstrip() + "…"


def ego_svg(c: _Ctx, e, rel: dict) -> str:
    """This name in the middle, the names it shares facts with around it: the line is as thick as the number of
    shared facts, which is written on the node. The EGO_MAX with most shared facts; the list beside has them all."""
    top = sorted(rel.items(), key=lambda x: (-x[1], x[0]))[:EGO_MAX]
    if not top:
        return ""
    w, h, cx, cy, r = 320, 250, 160, 122, 88
    most = max(k for _o, k in top)
    lines, nodes = [], []
    # an even count starts half a step off the top, so no node sits right below the centre, on its label
    start = -math.pi / 2 + (math.pi / len(top) if len(top) % 2 == 0 else 0.0)
    for i, (o, k) in enumerate(top):
        a = start + 2 * math.pi * i / len(top)
        x, y = cx + r * math.cos(a), cy + r * math.sin(a)
        name = str(c.ents[o].get("name") or "")
        kind = {"person": "p", "topic": "t"}.get(c.ents[o].get("type"), "u")
        width = 1 + 4 * k / most
        lines.append(f'<line class="mm-eg-edge" x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" stroke-width="{width:.1f}"/>')
        ty = y + (-17 if y < cy - 1 else 25)
        nodes.append(f'<a href="#mmap/e{int(o)}" class="mm-eg-node {kind}"><title>{esc(name)} · '
                     f'{esc(pl(k, _("{n} shared fact|{n} shared facts")))}</title>'
                     f'<circle cx="{x:.1f}" cy="{y:.1f}" r="12"/>'
                     f'<text class="mm-eg-k" x="{x:.1f}" y="{y + 4:.1f}">{k}</text>'
                     f'<text x="{x:.1f}" y="{ty:.1f}">{esc(_short(name))}</text></a>')
    me = str(e.get("name") or "")
    center = (f'<g class="mm-eg-me"><circle cx="{cx}" cy="{cy}" r="17"/>'
              f'<text x="{cx}" y="{cy + 35}">{esc(_short(me, 18))}</text></g>')
    more = len(rel) - len(top)
    label = _("Connections of {name}").format(name=me)
    return (f'<div class="mm-eg-fig"><svg viewBox="0 0 {w} {h}" role="img" aria-label="{esc(label)}">'
            + "".join(lines) + center + "".join(nodes) + '</svg>'
            + (f'<p class="mm-small">{_("+{n} more — all in the list").format(n=more)}</p>' if more > 0 else "")
            + '</div>')


def timeline(c: _Ctx, f: dict, versions: list) -> str:
    """The versions as bars on one time axis: each from when it was recorded to when it was replaced; the current
    one runs to the snapshot, a closed fact's last one stops where it was closed. A corrected version shows no text."""
    end_day = _day(c.snap.get("made"))
    spans = []
    for i, v in enumerate(versions):
        start = _day(v.get("rec"))
        if start is None:
            continue
        stop = _day(v.get("closed"))
        if stop is None and v.get("status") == "ended":
            stop = _day(v.get("to"))
        if stop is None and v.get("status") != "current" and i + 1 < len(versions):
            stop = _day(versions[i + 1].get("rec"))
        if stop is None:
            stop = end_day if v.get("status") == "current" else start
        spans.append((v, start, max(stop, start)))
    if not spans or end_day is None:
        return ""
    first = min(a for _v, a, _b in spans)
    last = max([end_day] + [b for _v, _a, b in spans])
    total = max(1, (last - first).days)
    pct = lambda d: 100.0 * (d - first).days / total  # noqa: E731
    rows = ""
    for v, a, b in spans:
        left = pct(a)
        width = max(1.5, pct(b) - left)
        width = min(width, 100.0 - left) if left < 100 else 0.0
        status = str(v.get("status") or "")
        if status == "corrected":
            label = _("a mistake, corrected")
        elif v.get("hidden"):
            label = _("hidden")
        else:
            label = str(v.get("text") or "")
        tip = _("version {n}").format(n=v.get("seq")) + " · " + version_status(status) + " · " + c.d(a.isoformat())
        tip += " — " + (c.d(b.isoformat()) if status != "current" else _("now"))
        cls = status if status in ("current", "changed", "ended", "corrected", "merged") else "changed"
        rows += (f'<div class="mm-track"><div class="mm-bar {cls}{" hid" if v.get("hidden") else ""}" '
                 f'style="left:{left:.1f}%;width:{width:.1f}%" title="{esc(tip)}">'
                 + (f'<span>{esc(label)}</span>' if status == "corrected" else esc(label)) + '</div></div>')
    return (f'<div class="mm-tl"><div class="mm-tl-axis"><span>{c.d(first.isoformat())}</span>'
            f'<span>{_("snapshot {d}").format(d=c.d(end_day.isoformat()))}</span></div>{rows}'
            f'<div class="mm-tl-key"><span><i class="current"></i>{_("in force")}</span>'
            f'<span><i class="changed"></i>{_("the past")}</span>'
            f'<span><i class="corrected"></i>{_("a corrected mistake, no text")}</span></div></div>')


def panel_fact(c: _Ctx, f) -> str:
    fid = f["id"]
    ents = [e for e in f.get("ents") or [] if e in c.ents]
    back = (f'<a class="mm-back" href="#mmap/e{int(ents[0])}">← {esc(c.ents[ents[0]].get("name"))}</a>' if ents
            else f'<a class="mm-back" href="#mmap/ents">← {_("People and topics")}</a>')
    flags = fact_flags(c, f)
    conf = [x for x in f.get("conf") or [] if isinstance(x, dict)]
    origin = origin_label(f.get("origin"))[1]
    rec = [c.d(f.get("rec")), via_label(f.get("via")), chat_label(f.get("chat"))]
    rec.append(_("from the words of {who}").format(who=esc(f.get("by"))) if f.get("by") else _("author of the message not recorded"))
    since = c.d(f.get("from")) if f.get("from") else _("{d} (no start date was named — counted from the record)").format(d=c.d(f.get("rec")))
    until = _("until {d}").format(d=c.d(f.get("to"))) if f.get("to") else _("to this day")
    recalls = _int(f.get("recalls"))
    used = (pl(recalls, _("came up {n} time|came up {n} times")) if recalls else _("never")) if recalls is not None else "—"
    helpful = _int(f.get("helpful"))
    if helpful:
        used += " · " + pl(helpful, _("marked useful {n} time|marked useful {n} times"))
    num = f.get("trust")
    trust = (f'{esc(trust_label(f.get("trust_level")))} ({num:.2f} {_("of 1")}). '
             if isinstance(num, (int, float)) else "")
    kv = (f'<dl class="mm-kv"><dt>{_("Who stands behind it")}</dt><dd>{esc(origin)}</dd>'
          f'<dt>{_("Recorded")}</dt><dd>{" · ".join(rec)}</dd>'
          f'<dt>{_("In force")}</dt><dd>{_("from")} {since} — {until}</dd>'
          f'<dt>{_("Confirmations")}</dt><dd>'
          + ("; ".join(c.d(x.get("d")) + (f' — {esc(x.get("by"))}' if x.get("by") else "") for x in conf) if conf
             else _("nobody confirmed it"))
          + f'</dd><dt>{_("Trust")}</dt><dd>{trust}{_("Built from the origin, confirmations and usefulness marks; it is not a probability of truth.")}</dd>'
          f'<dt>{_("Used")}</dt><dd>{used}</dd></dl>')
    versions = [v for v in f.get("versions") or [] if isinstance(v, dict)]
    vhtml = ""
    for v in versions:
        span = " ".join(x for x in [(_("from {d}").format(d=c.d(v.get("from"))) if v.get("from") else ""),
                                    (_("until {d}").format(d=c.d(v.get("to"))) if v.get("to") else "")] if x)
        if v.get("status") == "corrected":
            text = f'<span class="mm-dim">{_("The text of a corrected mistake is not shown: it never held.")}</span>'
        elif v.get("hidden"):
            text = f'<span class="mm-dim">{_("Hidden by the safety screen.")}</span>'
        else:
            text = esc(v.get("text"))
        vhtml += (f'<div class="card mm-ver {esc(v.get("status"))}"><div class="mm-m">'
                  + _("version {n}").format(n=esc(v.get("seq"))) + f' · {version_status(v.get("status"))} · '
                  + _("recorded {d}").format(d=c.d(v.get("rec"))) + (f' · {span}' if span else "")
                  + f'</div><div>{text}</div></div>')
    merged = [m for m in f.get("mergedFrom") or [] if isinstance(m, dict)]
    mhtml = "".join(f'<div class="card mm-ver"><div class="mm-m">№{esc(m.get("id"))} · {c.d(m.get("d"))}'
                    + (f' · {esc(m.get("prop"))}' if m.get("prop") else "") + '</div>'
                    + f'<div class="mm-dim">{esc(m.get("text")) if m.get("text") is not None else _("Hidden by the safety screen.")}</div></div>'
                    for m in merged)
    chips = "".join(ent_chip(c, e) for e in ents) + (f'<span class="mm-chip">{esc(f.get("cat"))}</span>' if f.get("cat") else "")
    return (back + f'<div class="mm-m">{_("Fact №{n}").format(n=int(fid))}</div>'
            f'<h2 class="mm-title">{esc(f.get("text"))}</h2>'
            f'<div class="mm-meta">{origin_chip(f.get("origin"))}{trust_chip(f, True)}{chips}</div>{flags}'
            + sec(_("Where it comes from"), "", kv)
            + (sec(_("Version history"), pl(len(versions), _("{n} version|{n} versions")),
                   timeline(c, f, versions) + vhtml) if versions else "")
            + (sec(_("Merged into it"), "", mhtml) if mhtml else "")
            + hint(_("Read-only. To change it, say in the chat: «fact №{n} is outdated: …», «№{n} is a mistake» or «forget №{n}».").format(n=int(fid))))


def panel_conflicts(c: _Ctx) -> str:
    if not c.conflicts:
        return f'<div class="card mm-small">{_("No contradictions between facts.")}</div>'
    out = (f'<p class="mm-lead">{_("Two facts about the same thing say different things. The agent does not choose by itself: both stay until a person answers.")}</p>')
    for x in c.conflicts:
        a, b = c.facts.get(x.get("a")), c.facts.get(x.get("b"))
        if not a or not b:
            continue
        rel = x.get("relies") if isinstance(x.get("relies"), dict) else {}
        on = rel.get("on")
        shared = "".join(ent_chip(c, e) for e in x.get("shared") or [])
        since = (f'<span class="mm-m">{_("both stand since {d}").format(d=c.d(x["since"]))}</span>'
                 if x.get("since") else "")
        if on in (a["id"], b["id"]):
            first, second = (a, b) if on == a["id"] else (b, a)
            pair = side(c, first["id"], _("Leans on now")) + side(c, second["id"], _("Disagrees"))
            why = _leans_why(c, rel, first["id"], second["id"])
            outdated, mistake = second["id"], first["id"]  # the side search does not lean on is the one to question
        else:
            first, second = (a, b) if str(a.get("rec") or "") <= str(b.get("rec") or "") else (b, a)
            pair = side(c, first["id"], _("Recorded earlier")) + side(c, second["id"], _("Recorded later"))
            outdated, mistake = first["id"], second["id"]
            why = {"tie": _("Search weighs both the same: which comes first depends on the question."),
                   "neither_found": _("Both are below the trust floor: search finds neither."),
                   "rerank": _("The reranker orders them by the question’s text: which comes first depends on the question."),
                   "settings_unknown": _("The memory settings could not be read, so the order is not known.")}.get(
                rel.get("why"), "")
        out += (f'<div class="card mm-pend"><div class="mm-row"><span class="pill p-no">{_("contradiction")}</span>'
                f'{shared}{since}</div><div class="mm-pair">{pair}</div>'
                + (f'<p class="mm-small">{esc(why)}</p>' if why else "")
                + _differ(x, a["id"], b["id"])
                + hint(_("Answer in the chat: «fact №{a} is outdated: …» or «№{b} is a mistake» — or «both are right».").format(
                    a=int(outdated), b=int(mistake))) + '</div>')
    return out


def _leans_why(c: _Ctx, rel: dict, first: int, second: int) -> str:
    sides = rel.get("sides") if isinstance(rel.get("sides"), dict) else {}
    one, two = sides.get(str(first)) or {}, sides.get(str(second)) or {}
    num = lambda v: f"{v:.2f}" if isinstance(v, (int, float)) and not isinstance(v, bool) else "?"  # noqa: E731
    if rel.get("why") == "floor":
        floor = (c.snap.get("ranking") or {}).get("min_trust") if isinstance(c.snap.get("ranking"), dict) else None
        return _("Search does not find №{b} at all: its trust {t} is below the floor {f}.").format(
            b=second, t=num(two.get("trust")), f=num(floor))
    text = _("When a question fits both equally, search puts №{a} first: trust {ta} against {tb}").format(
        a=first, ta=num(one.get("trust")), tb=num(two.get("trust")))
    if isinstance(one.get("decay"), (int, float)) and isinstance(two.get("decay"), (int, float)):
        text += _(", freshness {da} against {db}").format(da=num(one["decay"]), db=num(two["decay"]))
    return text + ". " + _("A question that fits the other’s words better can still bring it up first.")


def _differ(x: dict, a: int, b: int) -> str:
    d = x.get("differ") if isinstance(x.get("differ"), dict) else {}
    words = lambda k: [w for w in d.get(k) or [] if isinstance(w, str)]  # noqa: E731
    bits = []
    if words("a"):
        bits.append(_("only in №{n}: {w}").format(n=a, w=esc(", ".join(words("a")))))
    if words("b"):
        bits.append(_("only in №{n}: {w}").format(n=b, w=esc(", ".join(words("b")))))
    if d.get("negation") is True:
        bits.append(esc(_("one of them is the negation of the other")))
    return f'<p class="mm-small"><b>{_("What differs")}:</b> {" · ".join(bits)}</p>' if bits else ""


def panel_weak(c: _Ctx) -> str:
    groups: dict[str, list] = {}
    for f in c.facts.values():
        if f.get("status") != "current":
            continue
        for w in f.get("weak") or []:
            groups.setdefault(w, []).append(f)
    out = (f'<p class="mm-lead">{_("Facts the agent leans on with a reservation. These are candidates for «still relevant?», not mistakes; their cards are dashed everywhere.")}</p>')
    names = {"low_trust": _("Low trust"), "never_recalled": _("Never came up in 60+ days"),
             "unconfirmed_inference": _("Inferences nobody confirmed"), "near_duplicate": _("Almost repeat another fact"),
             "no_source": _("Source not recorded")}
    count = _counts(c)["weak"]
    if count:
        out += f'<p class="mm-small">{pl(count, _("{n} fact on a weak basis|{n} facts on a weak basis"))}</p>'
    for code, fs in sorted(groups.items()):
        out += sec(names.get(code, esc(code)), pl(len(fs), _("{n} fact|{n} facts")), "".join(fact_card(c, f) for f in fs))
    if not groups:
        out += f'<div class="card mm-small">{_("No facts on a weak basis.")}</div>'
    legacy = _int((c.snap.get("counts") or {}).get("legacy")) or 0
    if legacy:
        out += f'<p class="mm-foot">{pl(legacy, _("{n} old record without a source is not counted as weak — see «About the snapshot».|{n} old records without a source are not counted as weak — see «About the snapshot»."))}</p>'
    prov = [e for e in c.ents.values() if e.get("standing") == "provisional"]
    if prov:
        rows = "".join(f'<div class="card mm-pend"><div class="mm-row">{ent_chip(c, e["id"])}<span class="mm-m">'
                       + _("since {d}").format(d=c.d(e.get("since")))
                       + (f' · {_("unconfirmed for 60+ days")}' if e.get("stale") else "") + '</span></div>'
                       + hint(_("Confirm: «{name} is a name» · remove: «{name} is not a name» — the facts stay.").format(
                           name=esc(e.get("name")))) + '</div>' for e in prov)
        out += sec(_("Names guessed from one fact"), pl(len(prov), _("{n} name|{n} names")), rows)
    return out


def panel_pending(c: _Ctx) -> str:
    if not c.pending:
        return f'<div class="card mm-small">{_("Nothing waits for a decision.")}</div>'
    return (f'<p class="mm-lead">{_("The nightly review changes nothing by itself — it asks. A question stays here until you answer the agent in the chat.")}</p>'
            + "".join(pending_card(c, p) for p in c.pending))


def feed_row(c: _Ctx, x) -> str:
    cls, label = {"new": ("p-ok", _("new")), "chg": ("p-avail", _("changed")), "end": ("tg id", _("closed")),
                  "conf": ("p-ok", _("confirmed")), "del": ("p-no", _("forgotten")), "mrg": ("p-avail", _("merged")),
                  "night": ("p-part", _("night review"))}.get(x.get("k"), ("tg id", str(x.get("k") or "?")))
    k = x.get("k")
    if k in ("new", "chg", "end", "conf"):
        f = c.facts.get(x.get("f"))
        body = (fref(x["f"]) + " " + esc(f.get("text")) if f else "№" + esc(x.get("f")))
        if k == "chg" and x.get("how") == "corrected":
            body += f' <span class="mm-m">{_("a mistake was corrected")}</span>'
        if k == "conf" and x.get("by"):
            body += f' <span class="mm-m">— {esc(x["by"])}</span>'
    elif k == "del":
        body = _("Fact №{n} was forgotten — its text is gone.").format(n=esc(x.get("id")))
    elif k == "mrg":
        body = _("№{a} merged into {b}").format(a=esc(x.get("from")), b=fref(x.get("into")) if _int(x.get("into")) else "—")
    elif k == "night":
        alerts = [a for a in x.get("alerts") or [] if isinstance(a, str)]
        stats = x.get("stats") if isinstance(x.get("stats"), dict) else {}
        at = str(x.get("at") or "")
        body = _("Nightly review") + (f' {esc(at[11:16])}' if len(at) >= 16 else "") + " — "
        body += ("; ".join(f'<b class="mm-warn">{alert_label(a)}</b>' for a in alerts) if alerts else _("no warnings"))
        if stats:
            body += f' <span class="mm-m">{" · ".join(esc(k2) + " " + esc(v) for k2, v in stats.items())}</span>'
    else:
        body = esc(k)
    return f'<div class="evt"><span class="pill {cls}">{esc(label)}</span><span class="etx">{body}</span></div>'


def panel_week(c: _Ctx) -> str:
    if not c.feed:
        return f'<div class="card mm-small">{_("Nothing happened in the 7 days before the snapshot.")}</div>'
    days: dict[str, list] = {}
    for x in c.feed:
        days.setdefault(str(x.get("d") or ""), []).append(x)
    out = f'<p class="mm-lead">{_("{a} — {b}: what the agent learned, changed, confirmed and forgot, and what the nightly review did.").format(a=c.d(c.snap.get("week_from")), b=c.d(c.snap.get("made")))}</p>'
    out += (f'<p class="mm-small">{pl(len(c.new_week), _("{n} new fact|{n} new facts"))} · '
            f'{pl(len(c.feed), _("{n} event in all|{n} events in all"))}</p>')
    for d in sorted(days, reverse=True):
        out += sec(c.d(d), "", '<div class="card mm-feed">' + "".join(feed_row(c, x) for x in days[d]) + '</div>')
    return out


def panel_removed(c: _Ctx) -> str:
    tombs = [t for t in c.data["TOMBS"] if isinstance(t, dict)]
    hidden = [h for h in c.data["HIDDEN"] if isinstance(h, dict)]
    merges = [m for m in c.data["MERGES"] if isinstance(m, dict)]
    ended = [f for f in c.facts.values() if f.get("status") == "ended"]
    out = (f'<p class="mm-lead">{_("Four different things. Forgotten — the text is gone from memory, only the number and the date are left. Hidden — the safety screen keeps the text out of answers and out of this page. Merged — the copy became part of another fact’s history. Closed — no longer in force, the history stays.")}</p>')
    rows = "".join(f'<div class="mm-tomb"><b>№{esc(t.get("id"))}</b><span class="mm-m">{c.d(t.get("d"))}</span></div>' for t in tombs)
    out += sec(_("Forgotten on request"), pl(len(tombs), _("{n} fact|{n} facts")),
               (f'<div class="card">{rows}</div>' if rows else f'<div class="card mm-small">{_("none")}</div>'))
    if hidden:
        rows = "".join(f'<div class="mm-tomb"><b>№{esc(h.get("id"))}</b><span class="mm-m">{_("recorded {d}").format(d=c.d(h.get("d")))} · '
                       f'{screen_label(h.get("why"))}</span></div>' for h in hidden)
        out += sec(_("Hidden by the safety screen"), _("the agent does not use them in answers either"), f'<div class="card">{rows}</div>')
    if merges:
        rows = "".join(f'<div class="card mm-ver"><div class="mm-m">№{esc(m.get("from"))} → '
                       + (fref(m.get("into")) if _int(m.get("into")) else "—") + f' · {c.d(m.get("d"))}'
                       + (f' · {esc(m.get("prop"))}' if m.get("prop") else "") + '</div>'
                       + f'<div class="mm-dim">{esc(m.get("text")) if m.get("text") is not None else _("Hidden by the safety screen.")}</div></div>'
                       for m in merges)
        out += sec(_("Merged"), pl(len(merges), _("{n} fact|{n} facts")), rows)
    if ended:
        out += sec(_("No longer in force"), pl(len(ended), _("{n} fact|{n} facts")), "".join(fact_card(c, f) for f in ended))
    return out


def panel_about(c: _Ctx, every: int) -> str:
    s = c.snap
    counts = s.get("counts") if isinstance(s.get("counts"), dict) else {}
    n = lambda k: _int(counts.get(k)) or 0  # noqa: E731
    made = str(s.get("made") or "")
    inside = ", ".join([pl(n("current"), _("{n} current fact|{n} current facts")),
                        pl(n("ended"), _("{n} closed|{n} closed")), pl(n("merged"), _("{n} merged|{n} merged")),
                        pl(n("forgotten"), _("{n} forgotten, without text|{n} forgotten, without text")),
                        pl(n("corrected"), _("{n} corrected version, without text|{n} corrected versions, without text"))])
    inside += "; " + pl(n("entities"), _("{n} name|{n} names")) + (
        " (" + _("{n} not confirmed").format(n=n("provisional")) + ")" if n("provisional") else "")
    schema = s.get("schema") if isinstance(s.get("schema"), dict) else {}
    dreaming = s.get("dreaming") if isinstance(s.get("dreaming"), dict) else {}
    if dreaming.get("read"):
        night = _("read: questions and reports of the nightly review for this store")
    else:
        night = {"not_requested": _("not read: turned off in the dashboard settings"),
                 "other_store": _("not read: the nightly review works on another store")}.get(
            dreaming.get("reason"), _("not read"))
    left_out = dreaming.get("invalid") if isinstance(dreaming.get("invalid"), list) else []
    if left_out:  # the export names them (file, number, field); their contents stay out of the page
        night += "; " + pl(len(left_out), _("{n} record of another shape was left out|{n} records of another shape were left out"))
    legacy = n("legacy")
    kv = (f'<dl class="mm-kv"><dt>{_("Made")}</dt><dd>{c.d(made)} {esc(made[11:16])} ({esc(s.get("tz"))})</dd>'
          f'<dt>{_("Store")}</dt><dd>{esc(s.get("store"))}'
          + (" · " + esc(", ".join(f"{k} {v}" for k, v in schema.items() if v is not None)) if schema else "") + '</dd>'
          f'<dt>{_("Inside")}</dt><dd>{inside}</dd>'
          f'<dt>{_("Old records")}</dt><dd>'
          + (pl(legacy, _("{n} fact was written before memory recorded who said it. It is not counted as weak for that; its origin shows as unknown.|"
                          "{n} facts were written before memory recorded who said them. They are not counted as weak for that; their origin shows as unknown."))
             if legacy else _("none")) + '</dd>'
          f'<dt>{_("Hidden by the safety screen")}</dt><dd>{pl(n("hidden"), _("{n} fact, number and date only|{n} facts, number and date only"))}</dd>'
          f'<dt>{_("Nightly review")}</dt><dd>{night}</dd>'
          f'<dt>{_("Usage journal")}</dt><dd>{_("not included in this version")}</dd></dl>')
    freq = _("every {n} min").format(n=every) if every else _("on schedule")
    bounds = "".join(f'<p><b>{t}</b> {b}</p>' for t, b in [
        (_("Behind the dashboard’s password."), _("The tab is part of the dashboard page and makes no requests of its own.")),
        (_("Read-only."), _("There are no edit or delete buttons: every change to memory goes through the chat, so it has an author and a date.")),
        (_("A snapshot, rebuilt with the dashboard ({f}).").format(f=freq), _("What the agent learned or forgot after the moment above is not here yet; «forget» reaches this page on the next rebuild.")),
        (_("One store."), _("Stores of separate chats are not opened, not even counted.")),
        (_("Downloaded copies are outside «forget».") , _("If you save or forward this page, deleting that copy is up to you.")),
    ])
    legend = (f'<div class="mm-meta">{origin_chip("said")}{origin_chip("confirmed")}{origin_chip("inferred")}{origin_chip("unknown")}</div>'
              f'<p class="mm-small">{_("Who stands behind a fact: a person said it · the agent inferred it and a person agreed · the agent inferred it and nobody confirmed · recorded before this was tracked.")}</p>'
              f'<div class="mm-meta">{trust_chip({"trust_level": "strong"})}{trust_chip({"trust_level": "normal"})}{trust_chip({"trust_level": "weak"})}</div>'
              f'<p class="mm-small">{_("Trust in three words: weak below 0.45 · normal 0.45–0.6 · strong from 0.65. The number is on the fact card.")}</p>'
              f'<p class="mm-small">{_("A dashed card is a weak basis. A struck-through one is no longer in force. A red line is a contradiction.")}</p>')
    return (kv + sec(_("Boundaries"), "", f'<div class="card mm-bounds">{bounds}</div>')
            + sec(_("How to read a card"), "", f'<div class="card">{legend}</div>'))


# ── the view ──────────────────────────────────────────────────────────

# The "experimental" label beside the tab's title: shown also when the export failed, so it is not in memory-map.css
# (inlined only with a map), and not in style.css, so a dashboard with the tab off stays byte-identical (Codex review)
_LABEL_CSS = ("<style>.vh h1 .exp{display:inline-block;vertical-align:middle;margin-left:6px;font-family:var(--f-num);"
              "font-size:9.5px;font-weight:400;letter-spacing:.14em;text-transform:uppercase;line-height:1.5;"
              "color:var(--ink2);border:1px solid var(--amber);border-radius:999px;padding:1px 8px;"
              "white-space:nowrap}</style>")


def _style() -> str:
    try:
        return "<style>" + (ASSETS / "memory-map.css").read_text(encoding="utf-8") + "</style>" + _LABEL_CSS
    except OSError:
        return _LABEL_CSS


def _head(cfg: Config, made: str = "") -> str:
    meta = f'{_("source")} — <b>holographic · memory_map.py</b>'
    if made:
        meta += f' · {_("snapshot")} <b>{made}</b>'
    return (f'<div class="vh"><div class="kick">{_("What the agent remembers")}</div><h1>{_("Memory map")}'
            f' <span class="exp" title="{_("A new feature: its format and rules may still change. The memory plugin’s docs/memory-map.md lists its limits.")}">'
            f'{_("experimental")}</span></h1>'
            f'<p>{_("Facts with their source and trust, what contradicts itself, what stands on a weak basis, what waits for your decision and what was removed. Read-only; changes go through the chat.")}</p>'
            f'<div class="meta">{meta}</div></div>')


def unavailable(cfg: Config, err: MapUnavailable | None) -> str:
    code = err.code if err else "crash"
    why = {"not_configured": _("memory_map.plugin_dir is not set in dashboard.json"),
           "no_plugin": _("memory_map.py was not found in the plugin folder"),
           "no_store": _("the memory store file does not exist"),
           "timeout": _("the export did not finish in {s} s").format(s=f"{err.value:g}" if err and isinstance(err.value, (int, float)) else "?"),
           "not_run": _("the export could not be started"),
           "exit": _("the export failed (exit code {n})").format(n=esc(err.value) if err else "?"),
           "not_json": _("the export printed something that is not JSON"),
           "schema": _("the snapshot has a format this dashboard does not know")}.get(code, _("the view could not be drawn"))
    return (f'<section class="view" id="v-mmap">{_LABEL_CSS}{_head(cfg)}'
            f'<div class="card"><h3>{_("Memory map unavailable")}</h3>'
            f'<p class="mm-small" style="margin:0">{esc(why)}. {_("Nothing is shown rather than numbers that could be wrong; the dashboard log has the details.")}</p></div>'
            f'<div class="printonly">{_("system dashboard")} · {_("Memory map")}</div></section>')


NAV = ("home", "ents", "pending", "conflicts", "weak", "week", "removed", "about")


def view(cfg: Config, data: dict | None, err: MapUnavailable | None = None, every: int = 0) -> str:
    """The <section id="v-mmap">: sub-navigation and one panel per page; MM_JS switches them."""
    if data is None:
        return unavailable(cfg, err)
    c = _Ctx(cfg, data)
    n = _counts(c)
    labels = {"home": _("Overview"), "ents": _("People and topics"), "pending": _("Waits for your decision"),
              "conflicts": _("Contradictions"), "weak": _("Weak basis"), "week": _("This week"),
              "removed": _("Removed and closed"), "about": _("About the snapshot")}
    # the same numbers as the overview tiles (_counts), nothing added
    badge = {"pending": n["pending"], "conflicts": n["conflicts"], "weak": n["weak"], "week": n["new"],
             "removed": n["forgotten"]}
    on = ' class="on"'
    nav = "".join(f'<a href="#mmap/{key}" data-pg="{key}"{on if key == "home" else ""}>{esc(labels[key])}'
                  + (f'<b class="{"hot" if key in ("pending", "conflicts") else ""}">{badge[key]}</b>' if badge.get(key) else "")
                  + '</a>' for key in NAV)
    panels = {"home": panel_home(c, n), "ents": panel_ents(c), "pending": panel_pending(c),
              "conflicts": panel_conflicts(c), "weak": panel_weak(c), "week": panel_week(c),
              "removed": panel_removed(c), "about": panel_about(c, every)}
    body = "".join(f'<div class="mm-pg{" on" if key == "home" else ""}" data-pg="{key}">'
                   + (f'<h2 class="mm-title">{esc(labels[key])}</h2>' if key != "home" else "") + html + '</div>'
                   for key, html in panels.items())
    body += "".join(f'<div class="mm-pg" data-pg="e{int(eid)}">{panel_entity(c, e)}</div>' for eid, e in sorted(c.ents.items()))
    body += "".join(f'<div class="mm-pg" data-pg="f{int(fid)}">{panel_fact(c, f)}</div>' for fid, f in sorted(c.facts.items()))
    made = str(c.snap.get("made") or "")
    return (f'<section class="view" id="v-mmap">{_style()}{_head(cfg, (c.d(made) + " " + esc(made[11:16])).strip())}'
            f'<nav class="mm-nav">{nav}</nav>{body}'
            f'<div class="printonly">{_("system dashboard")} · {_("Memory map")}</div></section>')


# Sub-pages inside the view: #mmap/<page>. The shared view switcher only knows
# #<view>, so this script also turns the view on when such a hash is opened or
# followed. Plain ES5 like the rest of the page.
MM_JS = r"""
(function(){
  var root = document.getElementById("v-mmap"); if(!root) return;
  var pages = [].slice.call(root.querySelectorAll(".mm-pg"));
  var links = [].slice.call(root.querySelectorAll(".mm-nav a"));
  function open(pg){
    var ok = pages.some(function(p){ return p.getAttribute("data-pg") === pg; });
    if(!ok) pg = "home";
    var nav = /^e\d+$/.test(pg) ? "ents" : pg;
    pages.forEach(function(p){ p.classList.toggle("on", p.getAttribute("data-pg") === pg); });
    links.forEach(function(a){ a.classList.toggle("on", a.getAttribute("data-pg") === nav); });
  }
  function route(){
    var h = (location.hash || "").replace("#", "");
    if(h !== "mmap" && h.indexOf("mmap/") !== 0){
      // "Back" from the map to another view: the base script reads the hash only on load, so switch here
      if(root.classList.contains("on") && /^[a-z]+$/.test(h) && document.getElementById("v-" + h)){
        [].slice.call(document.querySelectorAll(".view")).forEach(function(v){ v.classList.toggle("on", v.id === "v-" + h); });
        [].slice.call(document.querySelectorAll("[data-v]")).forEach(function(b){ b.classList.toggle("on", b.getAttribute("data-v") === h); });
      }
      return false;
    }
    [].slice.call(document.querySelectorAll(".view")).forEach(function(v){ v.classList.toggle("on", v.id === "v-mmap"); });
    [].slice.call(document.querySelectorAll("[data-v]")).forEach(function(b){ b.classList.toggle("on", b.getAttribute("data-v") === "mmap"); });
    open(h.slice(5) || "home");
    return true;
  }
  addEventListener("hashchange", function(){ if(route()) scrollTo(0, 0); });
  [].slice.call(document.querySelectorAll('[data-v="mmap"]')).forEach(function(b){
    b.addEventListener("click", function(){ open("home"); });
  });
  route();
})();
"""


def script() -> str:
    return "<script>" + MM_JS + "</script>"
