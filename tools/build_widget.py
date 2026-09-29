#!/usr/bin/env python3
"""Render a questlog.gg skill build (JSON) as a static HTML widget. PROTOTYPE.

Usage:
    python3 tools/build_widget.py BUILD.json [--questlog ../questlogdb/aion2] [--page classes/chanter.md]

Prints an HTML fragment for an mdBook page: the hotbar, skill priority rows and
macros. Skill ids are resolved against the questlogdb scrape; icons are the
site's existing ones in src/images/classes/<class>/ (matched by skill name).
Styles live in css/custom.css (.build*).
"""

import argparse
import difflib
import glob
import html
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")

# questlog class id -> site class folder
CLASS_DIRS = {
    "assassin": "assassin",
    "chanter": "chanter",
    "cleric": "cleric",
    "gladiator": "gladiator",
    "elementalist": "spirit-master",
    "sorcerer": "sorcerer",
    "templar": "templar",
    "ranger": "ranger",
}
ICON_PREFIX = {"active": "skill", "passive": "passive", "stigma": "stigma"}
CATEGORIES = [("active", "Active"), ("stigma", "Stigma"), ("passive", "Passive")]
COLUMNS = 12
ROWS = 4
MOUSE_COLUMN = 11  # only its bottom slot exists


def slug(s):
    s = s.lower().replace("’", "").replace("'", "")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def strip_tags(s):
    return re.sub(r"<[^>]+>", "", s or "").strip()


def load_skills(questlog, class_id):
    skills = {}
    for f in glob.glob(os.path.join(questlog, class_id, "*", "*.json")):
        if f.endswith("index.json"):
            continue
        with open(f) as fh:
            d = json.load(fh)["result"]["data"]
        skills[d["id"]] = d
    if not skills:
        sys.exit(f"no skills for class {class_id!r} in {questlog}")
    return skills


class Widget:
    def __init__(self, build, skills, page):
        self.build = build
        self.skills = skills
        self.page = page
        self.site_class = CLASS_DIRS[build["classId"]]
        self.icon_dir = os.path.join(SRC, "images", "classes", self.site_class)
        self.icons = sorted(os.listdir(self.icon_dir))

    def icon(self, d):
        """Site icon for a skill, matched by name (tolerating small spelling differences)."""
        want = f"{ICON_PREFIX[d['subCategory']]}-{slug(d['name'])}.png"
        if want not in self.icons:
            same_kind = [i for i in self.icons if i.startswith(ICON_PREFIX[d["subCategory"]] + "-")]
            close = difflib.get_close_matches(want, same_kind, n=1, cutoff=0.85)
            if not close:
                return None
            print(f"icon: {d['name']!r} -> {close[0]}", file=sys.stderr)
            want = close[0]
        rel = os.path.relpath(os.path.join("images", "classes", self.site_class, want), os.path.dirname(self.page))
        return rel

    def skill(self, sid, badge=None, show_specs=False):
        """One skill icon with level badge, optional top-left badge and a hover card."""
        d = self.skills[sid]
        entry = self.build["skills"].get(sid, {})
        lvl = entry.get("lvl")
        chosen = set(entry.get("specializations") or [])
        specs = [s for s in d.get("specializations", []) if s["id"] in chosen]
        cat = d["subCategory"]
        src = self.icon(d)
        name = html.escape(d["name"])
        img = f'<img src="{src}" alt="{name}" loading="lazy">' if src else f'<span class="build-noicon">{name}</span>'
        top = ""
        if badge is not None:
            top = f'<b class="build-badge build-idx">{badge}</b>'
        elif show_specs and specs:
            top = f'<b class="build-badge build-specs">{len(specs)}</b>'
        lvl_badge = f'<b class="build-badge build-lvl">{lvl}</b>' if lvl else ""
        tags = strip_tags(specs[0]["spec"]) if specs else ""
        tip = [f'<b class="build-tip-name">{name}</b>',
               f'<span class="build-tip-meta">{cat.title()}{f" · Lv {lvl}" if lvl else ""}{f" · {html.escape(tags)}" if tags else ""}</span>']
        if specs:
            tip.append("<ul>" + "".join(f"<li>{html.escape(strip_tags(s['specialized']))}</li>" for s in specs) + "</ul>")
        return (f'<span class="build-skill build-{cat}" tabindex="0">{img}{top}{lvl_badge}'
                f'<span class="build-tip" role="tooltip">{"".join(tip)}</span></span>')

    def hotbar(self):
        by_slot = {}
        for sid, entry in self.build["skills"].items():
            if entry.get("slotId") and sid in self.skills:
                col, row = (int(x) for x in entry["slotId"].split("."))
                by_slot[(col, row)] = sid
        cells = []
        for col in range(1, COLUMNS + 1):
            for row in range(ROWS):
                if col == MOUSE_COLUMN and row > 0:
                    continue
                pos = f'style="grid-column:{col};grid-row:{ROWS - row}"'
                sid = by_slot.get((col, row))
                inner = self.skill(sid, show_specs=True) if sid else ""
                cells.append(f'<div class="build-slot{"" if sid else " build-empty"}" {pos}>{inner}</div>')
            label = "🖱" if col == MOUSE_COLUMN else str(col)
            cells.append(f'<div class="build-key" style="grid-column:{col};grid-row:{ROWS + 1}">{label}</div>')
        return f'<div class="build-hotbar">{"".join(cells)}</div>'

    def priority(self):
        order = [p["id"] for p in self.build.get("priority", []) if p["id"] in self.skills]
        rows = []
        for cat, label in CATEGORIES:
            ids = [sid for sid in order if self.skills[sid]["subCategory"] == cat]
            if not ids:
                continue
            items = "".join(f"<li>{self.skill(sid, badge=i)}</li>" for i, sid in enumerate(ids, 1))
            rows.append(f'<div class="build-prio"><span class="build-prio-label">{label}</span><ol>{items}</ol></div>')
        return f'<div class="build-panel"><div class="build-title">Skill Priority</div>{"".join(rows)}</div>'

    def macros(self):
        blocks = []
        for n, m in enumerate(self.build.get("macros", []), 1):
            steps = []
            for step in m.get("steps", []):
                sid = str(step["skillId"])
                if sid not in self.skills:
                    continue
                delay = step.get("delayMs") or 0
                d = f'<span class="build-delay">{delay}ms</span>' if delay else ""
                steps.append(f"<li>{self.skill(sid)}{d}</li>")
            name = html.escape(m.get("name") or f"Macro {n}")
            blocks.append(f'<div class="build-macro"><div class="build-macro-name">{name}</div><ol>{"".join(steps)}</ol></div>')
        if not blocks:
            return ""
        return f'<div class="build-panel"><div class="build-title">Macros</div>{"".join(blocks)}</div>'

    def render(self):
        title = html.escape(self.build.get("name") or "Build")
        return (f'<div class="build"><div class="build-panel"><div class="build-title">{title} build · Hotbar</div>'
                f"{self.hotbar()}</div>{self.priority()}{self.macros()}</div>")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("build")
    ap.add_argument("--questlog", default=os.path.join(ROOT, "..", "questlogdb", "aion2"))
    ap.add_argument("--page", default=None, help="page the widget goes on, relative to src/ (for icon paths)")
    args = ap.parse_args()
    with open(args.build) as f:
        build = json.load(f)
    page = args.page or f"classes/{CLASS_DIRS[build['classId']]}.md"
    widget = Widget(build, load_skills(args.questlog, build["classId"]), page)
    print(widget.render())


if __name__ == "__main__":
    main()
