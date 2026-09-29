#!/usr/bin/env python3
"""Render a questlog.gg skill build (JSON) as a static HTML widget. PROTOTYPE.

Usage:
    python3 tools/build_widget.py BUILD.json [--page classes/chanter.md]
    python3 tools/build_widget.py --check      # every compiled skill has an icon?

Prints an HTML fragment for an mdBook page: the hotbar, skill priority rows and
macros. Skill ids are resolved against data/skills.json (built by
tools/compile_skills.py); icons are the site's existing ones in
src/images/classes/<class>/, matched by skill name. Styles live in
css/custom.css (.build*).
"""

import argparse
import html
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
SKILLS = os.path.join(ROOT, "data", "skills.json")

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

# Skills whose questlog name differs from the name in the Google Doc (which
# the site's icon files are named after): (class, questlog name) -> icon slug.
ICON_ALIASES = {
    ("assassin", "Shadowstep"): "shadow-step",
    ("cleric", "Judgment Thunder"): "judgement-thunder",
    ("elementalist", "Element Unification"): "elemental-unification",
    ("elementalist", "Jointstrike: Destructive Attack"): "jointstrike-destruction",
    ("elementalist", "Summon: Ancient Spirit"): "ancient-spirit",
    ("elementalist", "Summon: Earth Spirit"): "earth-spirit",
    ("elementalist", "Summon: Fire Spirit"): "fire-spirit",
    ("elementalist", "Summon: Water Spirit"): "water-spirit",
    ("elementalist", "Summon: Wind Spirit"): "wind-spirit",
    ("gladiator", "Assault Strike"): "assault-stike",  # doc typo
    ("gladiator", "Experienced Counterstrike"): "experienced-counterattack",
    ("ranger", "Wind Vigor"): "wing-vigor",
    ("sorcerer", "Winter's Shackles"): "winters-shackle",
    ("templar", "Judgment"): "judgement",
}

CATEGORIES = [("active", "Active"), ("stigma", "Stigma"), ("passive", "Passive")]
COLUMNS = 12
ROWS = 4
MOUSE_COLUMN = 11  # only its bottom slot exists


class BuildError(Exception):
    pass


def slug(s):
    s = s.lower().replace("’", "").replace("'", "")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def load_skills():
    if not os.path.exists(SKILLS):
        raise BuildError(f"{SKILLS} not found; run tools/compile_skills.py first")
    with open(SKILLS) as f:
        return json.load(f)["skills"]


def icon_file(skill):
    """Path of a skill's icon relative to src/ (it may not exist)."""
    name = ICON_ALIASES.get((skill["class"], skill["name"]), slug(skill["name"]))
    return f"images/classes/{CLASS_DIRS[skill['class']]}/{ICON_PREFIX[skill['category']]}-{name}.png"


class Widget:
    def __init__(self, build, skills, page):
        self.build = build
        self.skills = skills
        self.page = page
        unknown = [sid for sid in build["skills"] if sid not in skills]
        if unknown:
            raise BuildError(f"build uses skill ids not in data/skills.json: {unknown}; re-scrape and recompile?")

    def icon(self, skill):
        rel = icon_file(skill)
        if not os.path.exists(os.path.join(SRC, rel)):
            raise BuildError(f"no icon for {skill['name']!r} ({rel}); add it to ICON_ALIASES")
        return os.path.relpath(rel, os.path.dirname(self.page))

    def skill(self, sid, badge=None, show_specs=False):
        """One skill icon with level badge, optional top-left badge and a hover card."""
        s = self.skills[sid]
        entry = self.build["skills"].get(sid, {})
        lvl = entry.get("lvl")
        chosen = {str(x) for x in entry.get("specializations") or []}
        specs = [text for spec_id, text in s["specializations"].items() if spec_id in chosen]  # game order
        cat = s["category"]
        name = html.escape(s["name"])
        top = ""
        if badge is not None:
            top = f'<b class="build-badge build-idx">{badge}</b>'
        elif show_specs and specs:
            top = f'<b class="build-badge build-specs">{len(specs)}</b>'
        lvl_badge = f'<b class="build-badge build-lvl">{lvl}</b>' if lvl else ""
        tip = [f'<b class="build-tip-name">{name}</b>',
               f'<span class="build-tip-meta">{cat.title()}{f" · Lv {lvl}" if lvl else ""}</span>']
        if specs:
            tip.append("<ul>" + "".join(f"<li>{html.escape(t)}</li>" for t in specs) + "</ul>")
        return (f'<span class="build-skill build-{cat}" tabindex="0">'
                f'<img src="{self.icon(s)}" alt="{name}" loading="lazy">{top}{lvl_badge}'
                f'<span class="build-tip" role="tooltip">{"".join(tip)}</span></span>')

    def hotbar(self):
        by_slot = {}
        for sid, entry in self.build["skills"].items():
            if entry.get("slotId"):
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
            ids = [sid for sid in order if self.skills[sid]["category"] == cat]
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


def check(skills):
    missing = [f"{s['class']}: {s['name']} ({icon_file(s)})" for s in skills.values()
               if not os.path.exists(os.path.join(SRC, icon_file(s)))]
    stale = [k for k in ICON_ALIASES if not any((s["class"], s["name"]) == k for s in skills.values())]
    for m in missing:
        print(f"no icon: {m}")
    for k in stale:
        print(f"unused alias: {k}")
    print(f"{len(skills) - len(missing)}/{len(skills)} skills have icons", file=sys.stderr)
    return 1 if missing or stale else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("build", nargs="?")
    ap.add_argument("--page", default=None, help="page the widget goes on, relative to src/ (for icon paths)")
    ap.add_argument("--check", action="store_true", help="check every compiled skill has an icon")
    args = ap.parse_args()
    skills = load_skills()
    if args.check:
        sys.exit(check(skills))
    if not args.build:
        ap.error("BUILD.json is required (or use --check)")
    with open(args.build) as f:
        build = json.load(f)
    page = args.page or f"classes/{CLASS_DIRS[build['classId']]}.md"
    print(Widget(build, skills, page).render())


if __name__ == "__main__":
    try:
        main()
    except BuildError as e:
        sys.exit(f"build widget failed: {e}")
