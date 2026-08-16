#!/usr/bin/env python3
"""List every English UI string the engine passes through _() so a locale file
can be checked for coverage:

    python tools/extract_strings.py            # all keys, one per line
    python tools/extract_strings.py ru         # keys MISSING in locales/ru.json
"""
from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "hermes_dashboard"
TABLE_KEYS = {"TOOL_LABELS": 1, "STATUS_LABEL": 0, "DRIFT_LABEL": 1, "FLAG_LABEL": 0,
              "TOKEN_TYPE_LABELS": 1}


def call_keys(src: str) -> set[str]:
    """Every `_( "literal" )` argument, via the AST.

    A regex cannot do this: the long help texts are written as adjacent string
    literals across several lines, and a regex would capture only the first
    fragment — the locale would then hold a key that never appears at runtime,
    and the page would silently fall back to English while the coverage check
    reported success.
    """
    keys: set[str] = set()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return keys
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_" and node.args
                and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            keys.add(node.args[0].value)
    return keys


def collect() -> set[str]:
    keys: set[str] = set()
    for f in PKG.rglob("*.py"):
        s = f.read_text(encoding="utf-8")
        keys |= call_keys(s)
        for name in TABLE_KEYS:
            m = re.search(name + r"\s*=\s*\{(.*?)\n\}", s, re.S)
            if not m:
                continue
            for line in m.group(1).splitlines():
                # "key": ("Label", "css") / "key": "Label"  — labels are the values with spaces or capitals
                for pair in re.findall(r'"[^"]+"\s*:\s*\(?\s*"([^"]+)"', line):
                    keys.add(pair)
    keys.update(["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
                 "w", "d", "h", "min", "no data", "since"])
    keys.update(schema_keys())
    return keys


def schema_keys() -> set[str]:
    """Labels and help texts of the settings form (data, not _() call sites)."""
    sys.path.insert(0, str(ROOT))
    from hermes_dashboard.settings_schema import BUDGET_HELP, SCHEMA
    out: set[str] = set()
    for section in SCHEMA:
        out.update([section["title"], section["note"]])
        for card in section["cards"]:
            out.add(card["title"])
            if card.get("intro"):
                out.add(card["intro"])
            for f in card["fields"]:
                out.add(f["label"])
                for opt in ("help", "add_label", "key_label", "val_label"):
                    if f.get(opt):
                        out.add(f[opt])
                for _v, text in f.get("options", []):
                    out.add(text)
                for sub in f.get("item", []):
                    out.add(sub["label"])
                    for _v, text in sub.get("options", []):
                        out.add(text)
    for _unit, help_txt in BUDGET_HELP.values():
        if help_txt:
            out.add(help_txt)
    return out


def main() -> int:
    keys = collect()
    if len(sys.argv) > 1:
        loc = PKG / "locales" / f"{sys.argv[1]}.json"
        have = json.loads(loc.read_text(encoding="utf-8")) if loc.is_file() else {}
        missing = sorted(k for k in keys if k not in have)
        for k in missing:
            print(k)
        print(f"# {len(missing)} missing of {len(keys)}", file=sys.stderr)
        return 1 if missing else 0
    for k in sorted(keys):
        print(k)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
