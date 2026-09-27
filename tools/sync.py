#!/usr/bin/env python3
"""Regenerate the book's src/ from the AION 2 Guide Google Doc.

Downloads the doc's .docx export (the doc is public) and rewrites every page,
SUMMARY.md, and src/images/. Those are all generated: edit the Google Doc, or
the rules/config in this file, never the Markdown directly. Other files in
src/ (app icons, manifest.webmanifest) are hand-maintained and left alone.

Usage:
    python3 tools/sync.py              # fetch the doc and regenerate src/
    python3 tools/sync.py --docx FILE  # use a local .docx export instead

Requires Pillow (pip install -r tools/requirements.txt).

The script fails loudly when the doc no longer matches what it knows how to
convert (a tab was added/renamed, an in-doc link it can't resolve, an unknown
stigma category color, ...). Update the config below and re-run.
"""

import argparse
import io
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from PIL import Image

DOC_ID = "1H__aoCLtcAiTToZff7uVGvj7nI4896lRHYegHFJhV-Q"
EXPORT_URL = f"https://docs.google.com/document/d/{DOC_ID}/export?format=docx"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")


@dataclass
class Tab:
    name: str  # tab title exactly as in the doc
    path: str  # output page, relative to src/
    title: str  # page H1 and sidebar entry
    depth: int | None  # sidebar nesting; None = unnumbered prefix chapter
    heading: tuple[str, ...] = ()  # page H1 lines, if different from title
    center: bool = False  # center the page H1


# Every tab in the doc, in order. The script refuses to run if the doc differs.
TABS = [
    Tab("AION 2", "README.md", "AION 2 Guide", None, heading=("Dynasty’s", "AION 2 Guide"), center=True),
    Tab("Checklist", "checklist.md", "Checklist", 0),
    Tab("FIRST WEEK/Guides", "first-week/index.md", "FIRST WEEK/Guides", 0),
    Tab("DAY 1 - 2", "first-week/day-1-2.md", "DAY 1 - 2", 1),
    Tab("REST OF THE WEEK", "first-week/rest-of-the-week.md", "REST OF THE WEEK", 1),
    Tab("Whelp's Guide", "first-week/whelps-guide.md", "Whelp's Guide", 1),
    Tab("Dungeons", "dungeons.md", "Dungeons", 0),
    Tab("Raids", "raids.md", "Raids", 0),
    Tab("Class Guides", "classes/index.md", "Class Guides", 0),
    Tab("Assassin", "classes/assassin.md", "Assassin", 1),
    Tab("Chanter", "classes/chanter.md", "Chanter", 1),
    Tab("Cleric", "classes/cleric.md", "Cleric", 1),
    Tab("Gladiator", "classes/gladiator.md", "Gladiator", 1),
    Tab("Spirit Master", "classes/spirit-master.md", "Spirit Master", 1),
    Tab("Sorcerer", "classes/sorcerer.md", "Sorcerer", 1),
    Tab("Templar", "classes/templar.md", "Templar", 1),
    Tab("Ranger", "classes/ranger.md", "Ranger", 1),
]

# Tabs whose bullet lists render as task-list checkboxes (the .docx export
# drops Google Docs checklists to plain bullets).
CHECKLIST_TABS = {"Checklist"}

# Links between tabs don't survive the export; they come through as blue,
# underlined text with no target. Map that text to a page (tab names are
# added automatically below).
INTERNAL_LINKS = {
    "Find your class guide": "classes/index.md",
    "START": "first-week/day-1-2.md",
}
INTERNAL_LINKS.update({t.name: t.path for t in TABS})
LINK_COLOR = "1155cc"

# Class-page tables, keyed by the section heading they sit under.
CLASS_TABLES = {
    "Skills": ("skill", ["Skill", "Icon", "Lvl", "Specialty"]),
    "Passives": ("passive", ["Passive", "Icon", "Lvl", "Description"]),
    "Stigmas": ("stigma", ["Stigma", "Icon", "Lvl", "Notes"]),
}
# Stigma rows are grouped by the color of their cell borders.
STIGMA_CATEGORIES = {
    "cf3d6b": "🟥 Mandatory",
    "4d83da": "🟦 Situational",
    "7d5258": "🟫 Don't use",
}

# Paragraphs centered in the doc (styled in css/custom.css).
CENTER_OPEN, CENTER_CLOSE = '<div class="center">', "</div>"

ICON_WIDTH = 96  # px stored; displayed at 40
IMAGE_MAX_WIDTH = 800  # screenshots/logos; the content column is ~750px

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "v": "urn:schemas-microsoft-com:vml",
    "o": "urn:schemas-microsoft-com:office:office",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}
W = "{%s}" % NS["w"]
R = "{%s}" % NS["r"]

YOUTUBE_RE = re.compile(r"(?:youtube\.com/watch\?(?:.*&)?v=|youtu\.be/)([\w-]{11})")
URL_RE = re.compile(r"https?://[^\s<>()]+[^\s<>().,;:!?]")


class SyncError(Exception):
    pass


# ---------------------------------------------------------------- docx model


@dataclass
class Seg:
    """A piece of paragraph content."""

    kind: str  # text | br | img | hr
    text: str = ""
    fmt: tuple = ()  # subset of ("b", "i", "u", "mark")
    link: str | None = None  # external URL, or "internal:<text>"
    rid: str | None = None  # image relationship id


def wval(el, tag):
    """Value of a w:<tag w:val=...> child, None if absent, "" if present without val."""
    child = el.find(W + tag) if el is not None else None
    if child is None:
        return None
    return child.get(W + "val", "")


def on(v):
    return v is not None and v not in ("0", "false", "none")


def run_fmt(rpr):
    fmt = []
    if on(wval(rpr, "b")):
        fmt.append("b")
    if on(wval(rpr, "i")):
        fmt.append("i")
    if on(wval(rpr, "u")):
        fmt.append("u")
    hl = wval(rpr, "highlight")
    shd = rpr.find(W + "shd") if rpr is not None else None
    fill = shd.get(W + "fill", "").lower() if shd is not None else ""
    if on(hl) or fill not in ("", "auto", "ffffff"):
        fmt.append("mark")
    return tuple(fmt)


def is_internal_link_run(rpr):
    color = wval(rpr, "color")
    return color is not None and color.lower() == LINK_COLOR and on(wval(rpr, "u"))


def para_segments(p, rels):
    segs = []

    def walk(el, link):
        for child in el:
            tag = child.tag
            if tag == W + "hyperlink":
                rid = child.get(R + "id")
                target = rels.get(rid) if rid else None
                walk(child, target or link)
            elif tag == W + "r":
                rpr = child.find(W + "rPr")
                fmt = run_fmt(rpr)
                rlink = link
                if rlink is None and is_internal_link_run(rpr):
                    rlink = "internal:"
                if rlink is not None:
                    fmt = tuple(f for f in fmt if f != "u")
                for c in child:
                    ctag = c.tag
                    if ctag == W + "t":
                        segs.append(Seg("text", c.text or "", fmt, rlink))
                    elif ctag == W + "tab":
                        segs.append(Seg("text", " ", fmt, rlink))
                    elif ctag == W + "noBreakHyphen":
                        segs.append(Seg("text", "-", fmt, rlink))
                    elif ctag == W + "br":
                        segs.append(Seg("br"))
                    elif ctag == W + "drawing":
                        for blip in c.iter("{%s}blip" % NS["a"]):
                            segs.append(Seg("img", rid=blip.get(R + "embed")))
                    elif ctag == W + "pict":
                        if any(r.get("{%s}hr" % NS["o"]) == "t" for r in c.iter()):
                            segs.append(Seg("hr"))
            elif tag in (W + "sdt", W + "sdtContent", W + "smartTag", W + "customXml", W + "ins"):
                walk(child, link)

    walk(p, None)
    # internal link text is only known once the run text is collected
    for s in segs:
        if s.link == "internal:":
            s.link = "internal"
    return segs


def pstyle(p):
    return wval(p.find(W + "pPr"), "pStyle") or ""


def has_sect(p):
    ppr = p.find(W + "pPr")
    return ppr is not None and ppr.find(W + "sectPr") is not None


def ptext(el):
    return "".join(t.text or "" for t in el.iter(W + "t"))


HEADING_LEVEL = {"Title": 0, "Heading1": 1, "Heading2": 2, "Heading3": 3, "Heading4": 4, "Heading5": 5, "Heading6": 6}


# ---------------------------------------------------------------- rendering


def clean(s):
    return s.replace("​", "").replace("\xa0", " ").replace("\u000b", " ")


def esc(s):
    s = s.replace("\\", "\\\\")
    for ch in "*_`~":
        s = s.replace(ch, "\\" + ch)
    return s.replace("<", "&lt;")


def slug(s):
    s = s.lower().replace("’", "").replace("'", "")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def youtube_id(url):
    m = YOUTUBE_RE.search(url or "")
    return m.group(1) if m else None


_titles = {}


def youtube_title(vid):
    if vid not in _titles:
        url = "https://www.youtube.com/oembed?format=json&url=" + urllib.parse.quote(
            f"https://www.youtube.com/watch?v={vid}", safe=""
        )
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                _titles[vid] = json.load(r)["title"]
        except Exception as e:
            raise SyncError(f"could not fetch title for YouTube video {vid}: {e}")
    return _titles[vid]


def embed(vid, title):
    t = title.replace("&", "&amp;").replace('"', "&quot;")
    return (
        f'<div class="video"><iframe src="https://www.youtube-nocookie.com/embed/{vid}" title="{t}" '
        'loading="lazy" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; '
        'picture-in-picture; web-share" referrerpolicy="strict-origin-when-cross-origin" allowfullscreen>'
        "</iframe></div>"
    )


def wrap(text, fmt):
    """Apply inline formatting, keeping surrounding whitespace outside the markers."""
    core = text.strip()
    if not core:
        return text
    lead = text[: len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()) :]
    if "i" in fmt:
        core = f"*{core}*"
    if "b" in fmt:
        core = f"**{core}**"
    if "u" in fmt:
        core = f"<u>{core}</u>"
    if "mark" in fmt:
        core = f"<mark>{core}</mark>"
    return lead + core + trail


class Page:
    def __init__(self, tab, ctx):
        self.tab = tab
        self.ctx = ctx
        self.blocks = []  # rendered markdown blocks
        self.list_open = False
        self.section = ""  # latest heading text (for class tables)
        self.images = 0

    # -- inline

    def rel(self, target):
        return os.path.relpath(target, os.path.dirname(self.tab.path) or ".")

    def inline(self, segs, videos=None, drop_fmt=()):
        """Render text segments to markdown. YouTube links are collected into `videos`."""
        # drop a lone "@" before a link (Google smart-chip artifact)
        segs = [s for s in segs if s.kind == "text"]
        for i, s in enumerate(segs):
            if s.link is None and s.text.strip() == "@" and i + 1 < len(segs) and segs[i + 1].link:
                s.text = ""
        # merge adjacent segments with equal formatting and link
        merged = []
        for s in segs:
            t = clean(s.text)
            if not t:
                continue
            fmt = tuple(f for f in s.fmt if f not in drop_fmt)
            if merged and merged[-1].fmt == fmt and merged[-1].link == s.link:
                merged[-1].text += t
            else:
                merged.append(Seg("text", t, fmt, s.link))
        out = []
        i = 0
        while i < len(merged):
            link = merged[i].link
            group = []
            while i < len(merged) and merged[i].link == link:
                group.append(merged[i])
                i += 1
            if link is None:
                for s in group:
                    text = esc(s.text)
                    if "u" not in s.fmt:  # bare URLs aren't auto-linked by mdBook
                        text = URL_RE.sub(lambda m: f"<{m.group(0).replace(chr(92), '')}>", text)
                    out.append(wrap(text, s.fmt))
                continue
            raw = "".join(s.text for s in group)
            lead = raw[: len(raw) - len(raw.lstrip())]
            trail = raw[len(raw.rstrip()) :]
            label = "".join(wrap(esc(s.text).replace("[", "\\[").replace("]", "\\]"), s.fmt) for s in group).strip()
            if link == "internal":
                key = raw.strip()
                if key not in INTERNAL_LINKS:
                    raise SyncError(
                        f"{self.tab.name}: in-doc link {key!r} has no target; add it to INTERNAL_LINKS"
                    )
                out.append(f"{lead}[{label}]({self.rel(INTERNAL_LINKS[key])}){trail}")
                continue
            vid = youtube_id(link)
            if vid:
                title = raw.strip()
                if URL_RE.fullmatch(title) or not title:
                    title = youtube_title(vid)
                label = esc(title).replace("[", "\\[").replace("]", "\\]")
                if videos is not None:
                    videos.append((vid, title))
                out.append(f"{lead}▶️ [{label}](https://www.youtube.com/watch?v={vid}){trail}")
            elif URL_RE.fullmatch(raw.strip()):
                out.append(f"{lead}<{raw.strip()}>{trail}")
            else:
                out.append(f"{lead}[{label}]({link}){trail}")
        text = "".join(out)
        text = re.sub(r"\*\*(\s*)\*\*", r"\1", text)  # adjacent bold runs
        return re.sub(r"[ \t]+", " ", text).strip()

    def lines(self, segs, videos=None, drop_fmt=()):
        """Split a paragraph at line breaks. Returns a list of lines; '' marks a blank line."""
        lines, cur = [], []
        for s in segs:
            if s.kind == "br":
                lines.append(self.inline(cur, videos, drop_fmt))
                cur = []
            elif s.kind == "text":
                cur.append(s)
        lines.append(self.inline(cur, videos, drop_fmt))
        return lines

    # -- blocks

    def add(self, block, tight=False):
        if tight and self.blocks and self.list_open:
            self.blocks[-1] += "\n" + block
        else:
            self.blocks.append(block)

    def paragraph_blocks(self, lines):
        """Group lines into paragraphs: blank lines split, single breaks become hard breaks."""
        paras, cur = [], []
        for ln in lines + [""]:
            if ln:
                cur.append(ln)
            elif cur:
                paras.append(cur)
                cur = []
        return ["\\\n".join(p) for p in paras]

    def block_start(self, text):
        # keep paragraph starts from turning into lists/headings/quotes
        text = re.sub(r"^(\d+)([.)])(\s)", r"\1\\\2\3", text)
        return re.sub(r"^([#>+-])(\s)", r"\\\1\2", text)

    def add_images(self, segs, name_hint=""):
        for s in segs:
            if s.kind == "img":
                self.images += 1
                section = "macro" if self.section.lower().startswith("macro") else "image"
                base = os.path.splitext(self.tab.path)[0].replace("/index", "")
                if base == "README":
                    base = "home"
                rel_img = f"images/{base}/{section}-{self.images}.png"
                self.ctx.save_image(s.rid, rel_img, IMAGE_MAX_WIDTH)
                alt = f"{self.tab.title} {'macro screenshot' if section == 'macro' else 'image'} {self.images}"
                self.add(f"![{alt}]({self.rel(rel_img)})")

    def paragraph(self, p, heading_levels):
        segs = para_segments(p, self.ctx.rels)
        if any(s.kind == "hr" for s in segs):
            self.list_open = False
            self.add("---")
        style = pstyle(p)
        numpr = p.find(f"{W}pPr/{W}numPr")
        videos = []

        if style in HEADING_LEVEL:
            first_link = next((i for i, s in enumerate(segs) if s.link or s.kind == "br"), None)
            if first_link is not None and segs[first_link].kind != "br":
                segs = segs[:first_link] + [Seg("br")] + segs[first_link:]
            plain = self.lines(segs, drop_fmt=("b",))  # headings are already bold
            lines = self.lines(segs, videos)
            while len(lines) > 1 and not lines[0]:  # leading line breaks
                lines.pop(0)
                plain.pop(0)
            head, rest = plain[0], lines[1:]
            if head:
                self.list_open = False
                self.section = ptext(p).strip()
                level = heading_levels.get(HEADING_LEVEL[style], 2)
                if self.blocks or not self.is_title_repeat(head):
                    self.add("#" * level + " " + head)
            else:
                rest = lines
            # text after a line break inside a heading is body text
            for block in self.paragraph_blocks(rest):
                self.add(self.block_start(block))
        elif numpr is not None:
            text = "\\\n".join(ln for ln in self.lines(segs, videos) if ln)
            if text:
                level = int(wval(numpr, "ilvl") or 0)
                fmt = self.ctx.list_format(wval(numpr, "numId"), level)
                if not self.list_open:
                    self.counters = {}
                self.counters = {k: v for k, v in self.counters.items() if k <= level}
                self.counters[level] = self.counters.get(level, 0) + 1
                marker = f"{self.counters[level]}. " if fmt not in ("bullet", "none") else "- "
                if marker == "- " and self.tab.name in CHECKLIST_TABS:
                    marker = "- [ ] "
                indent = self.list_indent(level, marker)
                self.add(indent + marker + text.replace("\\\n", "\\\n" + indent + " " * len(marker)), tight=True)
                self.list_open = True
        else:
            blocks = self.paragraph_blocks(self.lines(segs, videos))
            if blocks:
                self.list_open = False
            centered = wval(p.find(W + "pPr"), "jc") == "center"
            for block in blocks:
                block = self.block_start(block)
                self.add(f"{CENTER_OPEN}\n\n{block}\n\n{CENTER_CLOSE}" if centered else block)
        if videos:
            self.list_open = False
            for vid, title in videos:
                self.add(embed(vid, title))
        self.add_images(segs)

    def list_indent(self, level, marker):
        if level == 0:
            self.indents = [len(marker)]
            return ""
        indents = getattr(self, "indents", [2])[:level]
        while len(indents) < level:
            indents.append(2)
        indent = " " * sum(indents)
        self.indents = indents + [len(marker)]
        return indent

    # -- tables

    def cell_text(self, tc):
        parts = []
        for p in tc.findall(W + "p"):
            segs = para_segments(p, self.ctx.rels)
            lines = [ln for ln in self.lines(segs) if ln]
            if not lines:
                continue
            bullet = p.find(f"{W}pPr/{W}numPr") is not None
            for j, ln in enumerate(lines):
                parts.append(("• " if bullet and j == 0 else "") + ln)
        return "<br>".join(parts).replace("|", "\\|")

    def cell_images(self, tc):
        return [s.rid for p in tc.findall(W + "p") for s in para_segments(p, self.ctx.rels) if s.kind == "img"]

    def table(self, tbl):
        self.list_open = False
        rows = tbl.findall(W + "tr")
        header = [self.cell_text(tc).replace("**", "") for tc in rows[0].findall(W + "tc")]
        kind = None
        if len(header) > 1 and header[1] == "Icon":
            key = self.section.split(" ")[0]
            if key not in CLASS_TABLES:
                raise SyncError(f"{self.tab.name}: icon table under unknown section {self.section!r}")
            kind, header = CLASS_TABLES[key]
            header = list(header)
            if kind == "stigma":
                header.append("Category")
        out = []
        for tr in rows[1:]:
            cells = tr.findall(W + "tc")
            row = []
            for ci, tc in enumerate(cells):
                if kind and ci == 1:
                    rids = self.cell_images(tc)
                    name = ptext(cells[0]).strip()
                    if rids:
                        rel_img = f"images/{os.path.splitext(self.tab.path)[0]}/{kind}-{slug(name)}.png"
                        self.ctx.save_image(rids[0], rel_img, ICON_WIDTH)
                        row.append(f'<img src="{self.rel(rel_img)}" alt="{name}" width="40">')
                    else:
                        row.append("")
                    continue
                text = self.cell_text(tc)
                if kind and ci == 0 and text:
                    text = "**" + text.replace("**", "") + "**"
                row.append(text)
            if kind == "stigma":
                row.append(self.stigma_category(cells[0]))
            out.append(row)
        lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
        lines += ["| " + " | ".join(r) + " |" for r in out]
        self.add("\n".join(lines))

    def stigma_category(self, tc):
        left = tc.find(f"{W}tcPr/{W}tcBorders/{W}left")
        color = (left.get(W + "color") or "").lower() if left is not None else ""
        if color not in STIGMA_CATEGORIES:
            raise SyncError(
                f"{self.tab.name}: stigma {ptext(tc).strip()!r} has unknown border color {color!r}; "
                "add it to STIGMA_CATEGORIES"
            )
        return STIGMA_CATEGORIES[color]

    # -- page

    def is_title_repeat(self, text):
        text = re.sub(r"[*_]", "", text).strip().lower()
        return text in (self.tab.title.lower(), self.tab.name.lower())

    def render(self, elements):
        headings = [e for e in elements if e.tag == W + "p" and pstyle(e) in HEADING_LEVEL and ptext(e).strip()]
        if headings and elements.index(headings[0]) == 0 and self.is_title_repeat(clean(ptext(headings[0]))):
            headings = headings[1:]
        used = sorted({HEADING_LEVEL[pstyle(e)] for e in headings})
        heading_levels = {lvl: min(i + 2, 6) for i, lvl in enumerate(used)}
        for el in elements:
            if el.tag == W + "p":
                self.paragraph(el, heading_levels)
            elif el.tag == W + "tbl":
                self.table(el)
        heading = self.tab.heading or (self.tab.title,)
        if self.blocks:
            # drop leading lines that repeat the page heading
            first = self.blocks[0]
            wrapped = first.startswith(CENTER_OPEN)
            if wrapped:
                first = first[len(CENTER_OPEN) : -len(CENTER_CLOSE)].strip("\n")
            lines = first.split("\\\n")
            while lines and heading and re.sub(r"[*_]", "", lines[0]).strip().lower() in (
                *(h.lower() for h in heading), self.tab.name.lower()):
                lines.pop(0)
            if lines:
                first = "\\\n".join(lines)
                self.blocks[0] = f"{CENTER_OPEN}\n\n{first}\n\n{CENTER_CLOSE}" if wrapped else first
            else:
                self.blocks.pop(0)
        attrs = " { .center }" if self.tab.center else ""
        return f"# {'<br>'.join(heading)}{attrs}\n\n" + "\n\n".join(self.blocks) + "\n"


# ---------------------------------------------------------------- document


class Doc:
    def __init__(self, data):
        self.zip = zipfile.ZipFile(io.BytesIO(data))
        rels = ET.fromstring(self.zip.read("word/_rels/document.xml.rels"))
        self.rels = {}
        self.media = {}
        for r in rels:
            if r.get("TargetMode") == "External":
                self.rels[r.get("Id")] = r.get("Target")
            else:
                self.media[r.get("Id")] = "word/" + r.get("Target")
        self.body = ET.fromstring(self.zip.read("word/document.xml")).find(W + "body")
        numbering = ET.fromstring(self.zip.read("word/numbering.xml"))
        abstract = {a.get(W + "abstractNumId"): a for a in numbering.findall(W + "abstractNum")}
        self.numfmt = {}
        for num in numbering.findall(W + "num"):
            a = abstract[wval(num, "abstractNumId")]
            for lvl in a.findall(W + "lvl"):
                self.numfmt[(num.get(W + "numId"), int(lvl.get(W + "ilvl")))] = wval(lvl, "numFmt")
        self.written = set()

    def list_format(self, num_id, level):
        return self.numfmt.get((num_id, level), "bullet")

    def save_image(self, rid, rel_path, max_width):
        if rel_path in self.written:
            raise SyncError(f"two images map to {rel_path}")
        self.written.add(rel_path)
        im = Image.open(io.BytesIO(self.zip.read(self.media[rid])))
        im.load()
        if im.width > max_width:
            im = im.resize((max_width, round(im.height * max_width / im.width)), Image.LANCZOS)
        dest = os.path.join(SRC, rel_path)
        if os.path.exists(dest):
            old = Image.open(dest)
            if old.size == im.size and old.mode == im.mode and old.tobytes() == im.tobytes():
                return  # unchanged pixels: keep the file byte-identical
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        im.save(dest, optimize=True)

    def split_tabs(self):
        """Tabs start at a Title paragraph that ends a section (the tab's title page)."""
        tabs, cur = [], None
        for el in self.body:
            if el.tag == W + "p" and pstyle(el) == "Title" and has_sect(el):
                name = clean(ptext(el)).strip()
                if name:
                    cur = (name, [])
                    tabs.append(cur)
                    continue
                # an empty one is just a page break, and may hold an image
            if cur is not None and el.tag in (W + "p", W + "tbl"):
                cur[1].append(el)
        return tabs


def summary():
    lines = ["# Summary", ""]
    for t in TABS:
        if t.depth is None:
            lines += [f"[{t.title}]({t.path})", ""]
    for t in TABS:
        if t.depth is not None:
            lines.append("  " * t.depth + f"- [{t.title}]({t.path})")
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--docx", help="use a local .docx export instead of downloading")
    args = ap.parse_args()

    if args.docx:
        with open(args.docx, "rb") as f:
            data = f.read()
    else:
        print(f"Downloading {EXPORT_URL}", file=sys.stderr)
        with urllib.request.urlopen(EXPORT_URL, timeout=300) as r:
            data = r.read()

    doc = Doc(data)
    tabs = doc.split_tabs()
    found = [name for name, _ in tabs]
    expected = [t.name for t in TABS]
    if not found:
        raise SyncError("no tabs found in the export; the download may be incomplete, try again")
    if found != expected:
        raise SyncError(
            "doc tabs don't match TABS config.\n  expected: %s\n  found:    %s" % (expected, found)
        )

    pages = {}
    for tab, (_, elements) in zip(TABS, tabs):
        pages[tab.path] = Page(tab, doc).render(elements)
    pages["SUMMARY.md"] = summary()

    for path, text in pages.items():
        dest = os.path.join(SRC, path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "w") as f:
            f.write(text)

    # remove pages and images that are no longer generated
    for dirpath, _, files in os.walk(SRC):
        for fn in files:
            rel = os.path.relpath(os.path.join(dirpath, fn), SRC)
            keep = rel in pages if fn.endswith(".md") else rel in doc.written if rel.startswith("images/") else True
            if not keep:
                os.remove(os.path.join(dirpath, fn))
                print(f"removed {rel}", file=sys.stderr)
    for dirpath, dirs, files in os.walk(SRC, topdown=False):
        if not dirs and not files:
            os.rmdir(dirpath)

    print(f"Wrote {len(pages)} pages and {len(doc.written)} images.", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except SyncError as e:
        sys.exit(f"sync failed: {e}")
