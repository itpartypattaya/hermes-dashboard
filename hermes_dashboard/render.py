"""HTML skeleton of index.html: rail (sidebar), views, footer, scripts.

Everything the old bash heredoc did, but in Python: no `$`/backtick traps,
strings go through i18n, and the page is assembled from generator sections.
Two languages = two static files (index.html for the default language,
index.<lang>.html for the others) plus a small script that remembers the
choice in localStorage and redirects on first load.
"""
from __future__ import annotations

import base64
from pathlib import Path

from .common import esc
from .config import Config
from .i18n import _

ASSETS = Path(__file__).parent.parent / "assets"
FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
         '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
         '<link href="https://fonts.googleapis.com/css2?family=Unbounded:wght@300;700;900'
         '&family=Onest:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;700'
         '&display=swap" rel="stylesheet">')
THEME_KEY = "hd-theme"
LANG_KEY = "hd-lang"

_FAVICON_CACHE: dict[str, str] = {}
_MIME = {".ico": "image/x-icon", ".png": "image/png", ".svg": "image/svg+xml",
         ".gif": "image/gif", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def favicon_link(cfg: Config) -> str:
    """<link rel="icon"> as a data: URI.

    Inlined rather than referenced as a file because the same <head> is served
    from two places — the static out_dir and the settings server on another
    path — and a relative favicon.ico would 404 in one of them. agent.favicon
    is resolved in the agent home first, then next to the engine; empty or
    missing file = no icon at all (a broken <link> is worse than none).
    """
    rel = str(cfg.get("agent.favicon", "") or "")
    if not rel:
        return ""
    if rel in _FAVICON_CACHE:
        return _FAVICON_CACHE[rel]
    cands = [cfg.path_in_home(rel), ASSETS.parent / rel]
    out = ""
    for p in cands:
        try:
            raw = Path(p).read_bytes()
        except OSError:
            continue
        if len(raw) > 200_000:      # a huge icon on every page is a bug, not a design
            break
        mime = _MIME.get(Path(p).suffix.lower(), "application/octet-stream")
        out = ('<link rel="icon" type="' + mime + '" href="data:' + mime + ";base64,"
               + base64.b64encode(raw).decode("ascii") + '">')
        break
    _FAVICON_CACHE[rel] = out
    return out


def _svg(body: str) -> str:
    return ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
            'stroke-linecap="round" stroke-linejoin="round">' + body + '</svg>')


ICONS = {
    "over": _svg('<rect x="3" y="3" width="7" height="9"/><rect x="14" y="3" width="7" height="5"/>'
                 '<rect x="14" y="12" width="7" height="9"/><rect x="3" y="16" width="7" height="5"/>'),
    "cost": _svg('<path d="M12 2v20"/><path d="M17 6H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/>'),
    "mem": _svg('<path d="M12 3a4 4 0 0 0-4 4v1a3 3 0 0 0 0 6v2a4 4 0 0 0 8 0v-2a3 3 0 0 0 0-6V7a4 4 0 0 0-4-4z"/>'
                '<path d="M12 3v18"/>'),
    "cron": _svg('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/>'),
    "cfg": _svg('<path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h7M15 18h5"/><circle cx="15" cy="6" r="2"/>'
                '<circle cx="9" cy="12" r="2"/><circle cx="13" cy="18" r="2"/>'),
    "map": _svg('<path d="M9 3 3 6v15l6-3 6 3 6-3V3l-6 3-6-3z"/><path d="M9 3v15M15 6v15"/>'),
    "set": _svg('<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1'
                'M17 17l2.1 2.1M19.1 4.9L17 7M7 17l-2.1 2.1"/>'),
}


def read_style() -> str:
    try:
        return (ASSETS / "style.css").read_text(encoding="utf-8")
    except OSError:
        return ""


def view_list(cfg: Config) -> list[tuple[str, str]]:
    views = [("over", _("Overview")), ("cost", _("Cost")), ("mem", _("Memory")), ("cron", _("Automation"))]
    if cfg.get("views.config_map", True):
        views.append(("cfg", _("Config")))
    return views


def page_name(lang: str, cfg: Config, base: str = "index") -> str:
    return f"{base}.html" if lang == cfg.default_lang else f"{base}.{lang}.html"


def head(cfg: Config, title: str, lang: str, base: str = "index") -> str:
    """<head> with anti-FOUC theme + language redirect (only on the default page).

    `base` names the page family: index/connectors redirect to their own
    <base>.<lang>.html, the settings server switches language with ?lang=xx
    (it is a server, not a file), so it gets no redirect script.
    """
    langs = cfg.languages
    redirect = ""
    if len(langs) > 1 and base != "settings":
        # first load of the default page: honour a remembered language
        pages = ",".join(f'"{l}":"{page_name(l, cfg, base)}"' for l in langs)
        redirect = ('<script>try{var hl=localStorage.getItem("' + LANG_KEY + '"),cur="' + lang + '",'
                    'pg={' + pages + '};if(hl&&hl!==cur&&pg[hl]&&!location.hash.match(/nolang/)){'
                    'location.replace(pg[hl]+location.hash)}}catch(e){}</script>')
    return (
        f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex, nofollow">'
        f'<title>{esc(title)}</title>' + favicon_link(cfg)
        + '<script>try{var mt=localStorage.getItem("' + THEME_KEY + '")||'
        '(matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light");'
        'document.documentElement.setAttribute("data-theme",mt)}catch(e){}</script>'
        + redirect + FONTS + f'<style>{read_style()}</style></head><body class="hasrail">'
    )


def lang_switch(cfg: Config, lang: str, base: str = "index", link_prefix: str = "") -> str:
    langs = cfg.languages
    if len(langs) < 2:
        return ""
    items = ""
    for l in langs:
        on = " on" if l == lang else ""
        href = f"?lang={l}" if base == "settings" else link_prefix + page_name(l, cfg, base)
        items += f'<a class="lsw{on}" data-lang="{l}" href="{href}">{l.upper()}</a>'
    return f'<div class="langsw">{items}</div>'


def rail(cfg: Config, lang: str, active_view: str, gw_dot: str, gw_txt: str, sync: str,
         sha: str, commit_full: str, current_page: str = "index", link_prefix: str = "") -> str:
    name = cfg.get("agent.name", "Hermes")
    glyph = cfg.get("agent.glyph", "◆")
    host = cfg.get("agent.host_label", "")
    nav = ""
    for key, lbl in view_list(cfg):
        if current_page == "index":
            on = ' class="on"' if key == active_view else ""
            nav += f'<button data-v="{key}"{on}>{ICONS[key]}{esc(lbl)}</button>'
        else:
            nav += f'<a href="{link_prefix}{page_name(lang, cfg)}#{key}">{ICONS[key]}{esc(lbl)}</a>'
    if cfg.get("views.connectors", True):
        cls = ' class="cur"' if current_page == "connectors" else ""
        conn = link_prefix + page_name(lang, cfg, "connectors")
        nav += f'<a{cls} href="{conn}">{ICONS["map"]}{esc(_("Capability map"))}</a>'
    settings = cfg.get("paths.settings_url", "")
    if current_page == "settings":
        nav += f'<a class="cur" href="./">{ICONS["set"]}{esc(_("Settings"))}</a>'
    elif settings:
        nav += f'<a href="{esc(settings)}">{ICONS["set"]}{esc(_("Settings"))}</a>'
    return (
        '<aside class="rail">'
        f'<div class="brandbox"><div class="glyph">{esc(glyph)}</div><div class="nm"><b>{esc(name)}</b><span>{esc(host)}</span></div>'
        f'<button class="tbtn" id="themeBtn" title="{esc(_("Toggle theme"))}" aria-label="{esc(_("Toggle theme"))}">🌙</button></div>'
        + lang_switch(cfg, lang, current_page, link_prefix)
        + f'<nav>{nav}</nav>'
        '<div class="foot">'
        f'<span class="health"><span class="pip on" style="background:{gw_dot}"></span><b>Gateway {esc(gw_txt)}</b></span>'
        f'<span class="health"><span class="pip info"></span><b>{esc(_("Sync"))} {esc(sync)}</b></span>'
        f'<span class="health" title="{esc(commit_full)}"><span class="pip off"></span><b>{esc(_("Commit"))} {esc(sha)}</b></span>'
        '</div>'
        f'<div class="health" id="railHealth"><span class="pip on" style="background:{gw_dot}"></span><b>Gateway {esc(gw_txt)}</b></div>'
        '</aside>'
    )


def bottom_tabs(cfg: Config, lang: str, active_view: str, current_page: str = "index",
                link_prefix: str = "") -> str:
    tabs = ""
    for key, lbl in view_list(cfg):
        if current_page == "index":
            on = ' class="on"' if key == active_view else ""
            tabs += f'<button data-v="{key}"{on}>{ICONS[key]}{esc(lbl)}</button>'
        else:
            tabs += f'<a href="{link_prefix}{page_name(lang, cfg)}#{key}">{ICONS[key]}{esc(lbl)}</a>'
    if cfg.get("views.connectors", True):
        cls = ' class="on"' if current_page == "connectors" else ""
        conn = link_prefix + page_name(lang, cfg, "connectors")
        tabs += f'<a{cls} href="{conn}">{ICONS["map"]}{esc(_("Map"))}</a>'
    if current_page == "settings":
        tabs += f'<a class="on" href="./">{ICONS["set"]}{esc(_("Settings"))}</a>'
    return f'<nav class="segs">{tabs}</nav>'


SCRIPT = r"""
(function(){
  var reduce = matchMedia("(prefers-reduced-motion:reduce)").matches;
  var tb = document.getElementById("themeBtn");
  function ic(){ if(tb) tb.textContent = document.documentElement.getAttribute("data-theme") === "dark" ? "☀️" : "🌙"; }
  if(tb){ ic(); tb.onclick = function(){
    var d = document.documentElement, t = d.getAttribute("data-theme") === "dark" ? "light" : "dark";
    d.setAttribute("data-theme", t);
    try{ localStorage.setItem("__THEME_KEY__", t); }catch(e){}
    ic();
  }; }
  [].slice.call(document.querySelectorAll(".langsw a")).forEach(function(a){
    a.addEventListener("click", function(){ try{ localStorage.setItem("__LANG_KEY__", a.getAttribute("data-lang")); }catch(e){} });
  });
  var tiles = [].slice.call(document.querySelectorAll(".card, .kpi, .blk, .diagram, .ccard"));
  tiles.forEach(function(t){
    t.classList.add("tilebase");
    ["glow","rim"].forEach(function(c){ var s = document.createElement("span"); s.className = c; t.insertBefore(s, t.firstChild); });
  });
  if(!reduce && matchMedia("(pointer:fine)").matches){
    var pending = false, lx = 0, ly = 0;
    document.addEventListener("pointermove", function(e){
      document.body.classList.add("lit"); lx = e.clientX; ly = e.clientY;
      if(pending) return; pending = true;
      requestAnimationFrame(function(){
        pending = false;
        for(var i = 0; i < tiles.length; i++){
          var el = tiles[i];
          if(!el.offsetParent) continue;
          var r = el.getBoundingClientRect();
          if(r.bottom < -240 || r.top > innerHeight + 240) continue;
          el.style.setProperty("--mx", (lx - r.left) + "px");
          el.style.setProperty("--my", (ly - r.top) + "px");
        }
      });
    }, {passive:true});
  }
  var buttons = [].slice.call(document.querySelectorAll("[data-v]"));
  var views = [].slice.call(document.querySelectorAll(".view"));
  function replay(root){
    if(!root) return;
    root.querySelectorAll(".cols i, .dg .draw, .dg .node-in").forEach(function(el){
      var delay = el.style.animationDelay;
      el.style.animation = "none"; void el.getBoundingClientRect();
      el.style.animation = ""; el.style.animationDelay = delay;
    });
  }
  function show(key){
    views.forEach(function(v){ v.classList.toggle("on", v.id === "v-" + key); });
    buttons.forEach(function(b){ b.classList.toggle("on", b.dataset.v === key); });
    replay(document.getElementById("v-" + key));
    try{ history.replaceState(null, "", "#" + key); }catch(e){}
  }
  buttons.forEach(function(b){
    b.addEventListener("click", function(){
      var key = b.dataset.v;
      if(document.startViewTransition && !reduce){ document.startViewTransition(function(){ show(key); }); }
      else { show(key); }
      scrollTo({top:0, behavior: reduce ? "auto" : "smooth"});
    });
  });
  var h = (location.hash || "").replace("#","");
  if(h && document.getElementById("v-" + h)) show(h);
  replay(document.querySelector(".view.on"));
})();
"""


def script() -> str:
    return "<script>" + SCRIPT.replace("__THEME_KEY__", THEME_KEY).replace("__LANG_KEY__", LANG_KEY) + "</script>"


CONNECTORS_JS = """var q=document.getElementById('q'),btns=[].slice.call(document.querySelectorAll('.fbtn')),
cards=[].slice.call(document.querySelectorAll('.ccard')),secs=[].slice.call(document.querySelectorAll('.sec.filterable')),
empty=document.getElementById('empty'),filt='all';
function apply(){var term=q.value.trim().toLowerCase(),shown=0;
cards.forEach(function(c){var okF=filt==='all'||c.getAttribute('data-status')===filt;
var okQ=!term||c.textContent.toLowerCase().indexOf(term)>=0;var vis=okF&&okQ;c.style.display=vis?'':'none';if(vis)shown++;});
secs.forEach(function(s){var any=[].slice.call(s.querySelectorAll('.ccard')).some(function(c){return c.style.display!=='none';});s.style.display=any?'':'none';});
empty.style.display=shown?'none':'block';}
btns.forEach(function(b){b.onclick=function(){btns.forEach(function(x){x.classList.remove('on');});b.classList.add('on');filt=b.getAttribute('data-f');apply();};});
if(q){q.oninput=apply;}"""
