"""Tiny gettext-style i18n: English strings are the keys.

    from hermes_dashboard.i18n import _
    _("Sessions · 7 days")            # → translated for the active language

`locales/<lang>.json` maps English → translation; a missing entry falls back
to English, so a half-translated locale still renders. The active language is
process-wide (`set_lang`), because the build renders one language per pass and
generators are plain functions.
"""
from __future__ import annotations

import json
from pathlib import Path

LOCALES = Path(__file__).parent / "locales"
_lang = "en"
_table: dict[str, str] = {}
_missing: set[str] = set()


def available() -> list[str]:
    langs = ["en"]
    for p in sorted(LOCALES.glob("*.json")):
        if p.stem != "en":
            langs.append(p.stem)
    return langs


def set_lang(lang: str) -> None:
    global _lang, _table
    _lang = lang or "en"
    _table = {}
    p = LOCALES / f"{_lang}.json"
    if _lang != "en" and p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _table = {str(k): str(v) for k, v in data.items()}
        except (OSError, ValueError):
            _table = {}


def lang() -> str:
    return _lang


def _(s: str) -> str:
    if _lang == "en" or not s:
        return s
    t = _table.get(s)
    if t is None:
        _missing.add(s)
        return s
    return t


def missing() -> set[str]:
    """English keys requested without a translation in the current language."""
    return set(_missing)


def date_fmt(d) -> str:
    """Short day.month for axes and event rows (locale-neutral numeric)."""
    return d.strftime("%d.%m")
