#!/usr/bin/env python3
"""Compile the questlog.gg skill scrape into data/skills.json.

Usage:
    python3 tools/compile_skills.py [--questlog ../questlogdb/aion2]

Keeps only what the build widget (tools/build_widget.py) needs, keyed by skill id:

    {"skills": {"18100000": {"name": "Dark Crush", "class": "chanter",
                             "category": "active",
                             "specializations": {"18100010": "Absorbs 0.7% HP", ...}}}}

Output is sorted and has no timestamps, so recompiling unchanged data produces
an identical file. The scrape is checked first and the script fails loudly on
anything it can't trust (a skill in an index with no detail file, a duplicate
id, untranslated text, ...). Re-scrape with ../questlogdb's fetch scripts,
then re-run this.
"""

import argparse
import glob
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "skills.json")
CATEGORIES = ("active", "passive", "stigma")
UNTRANSLATED = re.compile(r"\bSTR_[A-Z0-9_]+\b")


def load(path):
    with open(path) as f:
        return json.load(f)["result"]["data"]


def clean(text):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", text or "")).strip()


def compile_skills(questlog):
    skills, problems = {}, []
    classes = sorted(d for d in os.listdir(questlog) if os.path.isdir(os.path.join(questlog, d)))
    if not classes:
        sys.exit(f"no class folders in {questlog}")
    for cls in classes:
        for category in CATEGORIES:
            folder = os.path.join(questlog, cls, category)
            index_path = os.path.join(folder, "index.json")
            if not os.path.exists(index_path):
                problems.append(f"{cls}/{category}: no index.json")
                continue
            listed = {s["id"]: s["name"] for s in load(index_path)["pageData"]}
            found = set()
            for path in sorted(glob.glob(os.path.join(folder, "*.json"))):
                if path == index_path:
                    continue
                d = load(path)
                where = os.path.relpath(path, questlog)
                sid = d["id"]
                found.add(sid)
                if sid in skills:
                    problems.append(f"{where}: duplicate id {sid}")
                if d.get("mainCategory") != cls or d.get("subCategory") != category:
                    problems.append(f"{where}: filed under {cls}/{category} but says "
                                    f"{d.get('mainCategory')}/{d.get('subCategory')}")
                name = clean(d.get("name"))
                specs = {}
                for s in d.get("specializations") or []:
                    effect = clean(s.get("specialized"))
                    if not effect:
                        problems.append(f"{where}: specialization {s.get('id')} has no effect text")
                    specs[str(s["id"])] = effect
                for text in [name, *specs.values()]:
                    if UNTRANSLATED.search(text):
                        problems.append(f"{where}: untranslated text {text!r}")
                if not name:
                    problems.append(f"{where}: no name")
                skills[sid] = {"name": name, "class": cls, "category": category, "specializations": specs}
            for sid in listed.keys() - found:
                problems.append(f"{cls}/{category}: {listed[sid]!r} ({sid}) is in index.json but has no detail file")
            for sid in found - listed.keys():
                problems.append(f"{cls}/{category}: {sid} has a detail file but isn't in index.json")
    return skills, problems


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--questlog", default=os.path.join(ROOT, "..", "questlogdb", "aion2"),
                    help="scrape folder with <class>/<category>/*.json (default ../questlogdb/aion2)")
    args = ap.parse_args()

    skills, problems = compile_skills(args.questlog)
    if problems:
        sys.exit("compile failed:\n  " + "\n  ".join(problems))

    ordered = {sid: skills[sid] for sid in sorted(skills, key=int)}
    text = json.dumps({"skills": ordered}, indent=1, ensure_ascii=False, sort_keys=False) + "\n"
    old = open(OUT).read() if os.path.exists(OUT) else None
    if text == old:
        print(f"{os.path.relpath(OUT, ROOT)} is up to date ({len(skills)} skills)", file=sys.stderr)
        return
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        f.write(text)
    counts = {}
    for s in skills.values():
        counts[s["class"]] = counts.get(s["class"], 0) + 1
    print(f"wrote {os.path.relpath(OUT, ROOT)}: {len(skills)} skills, "
          f"{sum(len(s['specializations']) for s in skills.values())} specializations", file=sys.stderr)
    print("  " + ", ".join(f"{c} {n}" for c, n in sorted(counts.items())), file=sys.stderr)


if __name__ == "__main__":
    main()
