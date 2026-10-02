#!/usr/bin/env python3
"""Static site generator for the Might Make Sense wiki.

Sources live in `notes/`:
  notes/index.md   home page, Markdown, rendered to wiki_html/index.html
  notes/NNN.txt    one note per file in the field format of
                   notes/templates/note.txt, rendered to wiki_html/NNN.html

Note files are records of `Field: value` lines. `#` lines are comments,
`Description` is a bare header whose body runs until the next field, and a
note's ID is its file name (000-999).

Links between pages:
  [[001]] or [[001|display text]]   -> that note's page
  [[note title]]                    -> matched case-insensitively on Title
  Related: 001, 002                 -> rendered as links titled by target

Run: python3 build.py
"""

import html
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Match, Optional

ROOT: Path = Path(__file__).resolve().parent
SRC: Path = ROOT / "notes"
OUT: Path = ROOT / "wiki_html"
TEMPLATE_PATH: Path = SRC / "templates" / "page.html"
INDEX_PATH: Path = SRC / "index.md"
SITE_TITLE: str = "Might Make Sense"

FIELDS: tuple[str, ...] = ("ID", "Title", "Subtitle", "Description", "Links", "Related")
FIELD_RE: re.Pattern[str] = re.compile(
    r"^(" + "|".join(FIELDS) + r")\s*:?\s*(.*)$", re.IGNORECASE
)
NOTE_FILENAME_RE: re.Pattern[str] = re.compile(r"^\d{3}\.txt$")
NOTES_PLACEHOLDER: str = "{notes}"
FIELD_BY_KEY: dict[str, str] = {name.lower(): name for name in FIELDS}
LABELLED_URL_RE: re.Pattern[str] = re.compile(
    r"^(?P<label>[^:]+):\s*(?P<url>\w+://\S+)$"
)
LINK_LABELS: tuple[str, ...] = ("Short", "Extended")

# Back-to-index link on every note page: a house drawn in currentColor.
HOME_ICON: str = (
    '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" '
    'stroke="currentColor" stroke-width="1.75" stroke-linecap="round" '
    'stroke-linejoin="round" aria-hidden="true">'
    '<path d="M3 10.5 12 3l9 7.5"/>'
    '<path d="M5.5 9.5V20h13V9.5"/>'
    '<path d="M10 20v-5.5h4V20"/>'
    "</svg>"
)
HOME_LINK: str = (
    f'<p class="home"><a href="index.html" aria-label="Home" title="Home">'
    f"{HOME_ICON}</a></p>"
)

# lowercased ID or title -> slug
LinkTargets = dict[str, str]


@dataclass
class Note:
    nid: str
    path: Path
    title: str = ""
    subtitle: str = ""
    description: list[str] = field(default_factory=list)  # paragraphs
    links: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return self.nid


def warn(msg: str) -> None:
    print(f"warning: {msg}", file=sys.stderr)


def slugify(title: str) -> str:
    s: str = title.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-") or "untitled"


def split_values(raw: str) -> list[str]:
    return [v.strip() for v in re.split(r"[,\n]", raw) if v.strip()]


def parse_note(path: Path) -> Note:
    """Parse one NNN.txt record into a Note."""
    note = Note(nid=path.stem, path=path)
    raw: dict[str, list[str]] = {name: [] for name in FIELDS}
    current: Optional[str] = None

    for line in path.read_text().splitlines():
        if line.lstrip().startswith("#"):
            continue
        m: Optional[Match[str]] = FIELD_RE.match(line.strip())
        if m:
            current = FIELD_BY_KEY[m.group(1).lower()]
            value: str = m.group(2).strip()
            if value:
                raw[current].append(value)
            continue
        if current is None:
            continue
        raw[current].append(line.strip())

    declared: str = " ".join(raw["ID"]).strip()
    if declared and declared != note.nid:
        warn(f"{path.name}: ID field '{declared}' ignored; the file name is the ID")

    note.title = " ".join(raw["Title"]).strip()
    note.subtitle = " ".join(raw["Subtitle"]).strip()

    # description: blank lines separate paragraphs
    para: list[str] = []
    for line in raw["Description"]:
        if line:
            para.append(line)
        elif para:
            note.description.append(" ".join(para))
            para = []
    if para:
        note.description.append(" ".join(para))

    note.links = split_values("\n".join(raw["Links"]))
    note.related = split_values("\n".join(raw["Related"]))

    if not note.title:
        warn(f"{path.name}: no Title")
    return note


def load_notes() -> list[Note]:
    notes: list[Note] = []
    for path in sorted(SRC.iterdir()):
        if path.is_dir() or path == INDEX_PATH:
            continue
        if not NOTE_FILENAME_RE.match(path.name):
            warn(f"ignored {path.name}: does not match NNN.txt")
            continue
        notes.append(parse_note(path))
    return notes


def notes_grid_html(notes: list[Note]) -> str:
    items: list[str] = ['<ul class="notes-grid">']
    for note in notes:
        label: str = f"{note.nid} | {note.title or note.nid}"
        items.append(f'<li><a href="{note.slug}.html">{html.escape(label)}</a></li>')
    items.append("</ul>")
    return "\n".join(items)


def link_targets(notes: list[Note]) -> LinkTargets:
    targets: LinkTargets = {"index": "index"}
    for note in notes:
        targets[note.nid.lower()] = note.slug
        if note.title:
            targets.setdefault(note.title.lower(), note.slug)
    return targets


def inline(text: str, targets: LinkTargets) -> str:
    def wikilink(m: Match[str]) -> str:
        target: str = m.group(1)
        label: str
        if "|" in target:
            target, label = target.split("|", 1)
        else:
            label = target
        key: str = target.strip().lower()
        if key not in targets:
            warn(f"unresolved wikilink [[{target.strip()}]]")
        slug: str = targets.get(key, slugify(target))
        return f'<a href="{slug}.html">{html.escape(label.strip())}</a>'

    text = re.sub(r"\[\[([^\]]+)\]\]", wikilink, text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return text


def render_markdown(
    src: str, targets: LinkTargets, raw_blocks: Optional[dict[str, str]] = None
) -> str:
    raw_blocks = raw_blocks or {}
    lines: list[str] = src.splitlines()
    out: list[str] = []
    para: list[str] = []
    list_buf: list[str] = []

    def flush_para() -> None:
        if para:
            out.append("<p>" + inline(" ".join(para), targets) + "</p>")
            para.clear()

    def flush_list() -> None:
        if list_buf:
            out.append("<ul>")
            for item in list_buf:
                out.append(f"<li>{inline(item, targets)}</li>")
            out.append("</ul>")
            list_buf.clear()

    for line in lines:
        stripped: str = line.strip()
        if not stripped:
            flush_para()
            flush_list()
            continue

        if stripped in raw_blocks:
            flush_para()
            flush_list()
            out.append(raw_blocks[stripped])
            continue

        heading: Optional[Match[str]] = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            flush_para()
            flush_list()
            level: int = len(heading.group(1))
            out.append(f"<h{level}>{inline(heading.group(2), targets)}</h{level}>")
            continue

        if re.match(r"^-{3,}$", stripped):
            flush_para()
            flush_list()
            out.append("<hr>")
            continue

        list_item: Optional[Match[str]] = re.match(r"^[-*]\s+(.*)$", stripped)
        if list_item:
            flush_para()
            list_buf.append(list_item.group(1))
            continue

        flush_list()
        para.append(stripped)

    flush_para()
    flush_list()
    return "\n\n".join(out)


def render_note(note: Note, notes_by_id: dict[str, Note], targets: LinkTargets) -> str:
    out: list[str] = [f"<h1>{inline(note.title or note.nid, targets)}</h1>"]

    if note.subtitle:
        out.append(f'<p class="subtitle">{inline(note.subtitle, targets)}</p>')

    for paragraph in note.description:
        out.append(f'<p class="description">{inline(paragraph, targets)}</p>')

    if note.links:
        items: list[str] = ['<ul class="links">']
        for i, raw in enumerate(note.links):
            m: Optional[Match[str]] = LABELLED_URL_RE.match(raw)
            if m:
                label, url = m.group("label").strip(), m.group("url")
            else:
                label = LINK_LABELS[i] if i < len(LINK_LABELS) else f"Link {i + 1}"
                url = raw
            items.append(
                f'<li><a href="{html.escape(url)}">{html.escape(label)}</a></li>'
            )
        items.append("</ul>")
        out.append("\n".join(items))

    if note.related:
        rendered: list[str] = []
        for rid in note.related:
            target: Optional[Note] = notes_by_id.get(rid.lower())
            if target is None:
                warn(f"{note.path.name}: Related ID '{rid}' has no matching note")
                rendered.append(html.escape(rid))
                continue
            label = target.title or target.nid
            rendered.append(f'<a href="{target.slug}.html">{html.escape(label)}</a>')
        out.append('<p class="related">Related: ' + ", ".join(rendered) + "</p>")

    out.append(HOME_LINK)
    return "\n\n".join(out)


def write_page(template: str, slug: str, title: str, body: str) -> None:
    page: str = template.replace("{{ title }}", html.escape(title)).replace(
        "{{ content }}", body
    )
    (OUT / f"{slug}.html").write_text(page)
    print(f"wrote {OUT.name}/{slug}.html")


def build() -> None:
    OUT.mkdir(exist_ok=True)
    template: str = TEMPLATE_PATH.read_text()

    notes: list[Note] = load_notes()
    notes_by_id: dict[str, Note] = {n.nid.lower(): n for n in notes}
    targets: LinkTargets = link_targets(notes)

    if INDEX_PATH.exists():
        body: str = render_markdown(
            INDEX_PATH.read_text(),
            targets,
            raw_blocks={NOTES_PLACEHOLDER: notes_grid_html(notes)},
        )
        write_page(template, "index", SITE_TITLE, body)
    else:
        warn(f"no {INDEX_PATH.name}; the site has no home page")

    for note in notes:
        write_page(
            template,
            note.slug,
            note.title or note.nid,
            render_note(note, notes_by_id, targets),
        )


if __name__ == "__main__":
    build()
