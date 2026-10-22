#!/usr/bin/env python3
"""Make the cover thumbnail for a note's video: one clear picture of the episode's main theme,
with the title set over it in a fixed font.

Usage:
  python3 animate/thumbnail.py 001             # draw the picture if needed, then set the title
  python3 animate/thumbnail.py 001 --redraw    # draw a new picture (the old one goes to history/)
  python3 animate/thumbnail.py 001 --dry-run   # write/print the prompt, no API calls

Two steps, on purpose:
  1. Picture: the image model draws the theme as one bold visual metaphor (prompts/thumbnail.txt),
     from the series references and the storyboard's cast, leaving the top of the image empty.
     Saved as out/<id>/thumbnail-art.* (+ thumbnail-art.txt, the prompt). ~$0.045.
  2. Title: set over the empty top band with Pillow, in FONT. Image models misspell text and
     never draw the same lettering twice, so the title is never left to the model. Free, and
     redone on every run, so a changed title is picked up without redrawing.
Output: out/<id>-thumbnail.png (1080x1920), next to the MP4.

animate.py runs this after the frames (`just animate` / `just video`; --no-thumbnail to skip).
"""

import argparse
import shutil
import sys
from pathlib import Path

from animate import (IMAGE_COST, IMAGE_EXTS, OUT, cast_text, generate_image, load_board, load_prompt,
                     read_note, references, run_cli, script_text)

HERE = Path(__file__).resolve().parent

W, H = 1080, 1920
# Cover layout, as fractions of the 9:16 image. Profile grids show roughly the middle 3:4
# (y 12.5%-87.5%), so the title and the picture both sit inside that.
TITLE_TOP, TITLE_BOTTOM = 0.14, 0.37  # the title is set in this band, which the picture leaves empty
ART_TOP, ART_BOTTOM = 0.41, 0.86      # the picture fills this band (a gap below the title: models draw a bit high)
MARGIN = 0.08                         # left/right margin for the title and the picture

# Title type: fixed, the same on every cover.
FONT = "Fredoka"        # animate/fonts/Fredoka.ttf, committed with its licence (OFL.txt); or any .ttf there
FONT_WEIGHT = "Bold"    # named instance of a variable font (Fredoka: Light .. Bold); None for a static font
MAX_SIZE, MIN_SIZE = 190, 70  # px; the largest size that fits the band is used
LINE_SPACING = 1.08
MAX_LINES = 4
UPPERCASE = False
TEXT_COLOUR = (0, 0, 0)
STROKE_COLOUR, STROKE = (255, 255, 255), 10  # a white halo, so stray ink never touches the letters

TITLE_BAND_INK_MAX = 0.03  # dark pixels allowed in the title band of the picture before it's redrawn
MAX_ATTEMPTS = 2

FONT_DIRS = (HERE / "fonts", Path("/System/Library/Fonts/Supplemental"), Path("/Library/Fonts"),
             Path.home() / "Library/Fonts", Path("/usr/share/fonts/truetype/msttcorefonts"))


def font_path() -> Path:
    for d in FONT_DIRS:
        for ext in (".ttf", ".otf", ".ttc"):
            if (p := d / f"{FONT}{ext}").exists():
                return p
    sys.exit(f"thumbnail font {FONT!r} not found; drop {FONT}.ttf into animate/fonts/")


def find_art(outdir: Path) -> Path | None:
    return next((p for ext in IMAGE_EXTS if (p := outdir / f"thumbnail-art{ext}").exists()), None)


def art_prompt(title: str, lines: list[str], board: dict | None) -> str:
    pct = lambda f: f"{f:.0%}"
    return load_prompt(
        "thumbnail", title=title, script=script_text(lines),
        cast=cast_text(board) if board else "The main character from the series bible.",
        art_top_pct=pct(ART_TOP), art_bottom_pct=pct(ART_BOTTOM),
        art_left_pct=pct(MARGIN), art_right_pct=pct(1 - MARGIN))


def title_band_ink(path: Path) -> float:
    """Fraction of dark pixels in the title band: the picture should leave it empty."""
    import numpy as np
    from PIL import Image

    im = np.asarray(Image.open(path).convert("L"), dtype=np.float32) / 255
    h = im.shape[0]
    return float((im[int(TITLE_TOP * h):int(TITLE_BOTTOM * h)] < 0.5).mean())


def draw_art(outdir: Path, prompt: str) -> Path:
    """Draw the picture; redraw once if it intrudes on the title band. The best try is kept."""
    images = references()
    (outdir / "thumbnail-art.txt").write_text(
        "".join(f"[Image {k}] {label}  ({p.name})\n" for k, (label, p) in enumerate(images, 1)) + "\n" + prompt)
    tries = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(f"thumbnail: drawing the picture (try {attempt})...", flush=True)
        path = generate_image(prompt, images, outdir, f"thumbnail-art.try{attempt}")
        ink = title_band_ink(path)
        tries.append((ink, path))
        if ink <= TITLE_BAND_INK_MAX:
            break
        print(f"thumbnail: {ink:.1%} ink in the title band (max {TITLE_BAND_INK_MAX:.0%})", flush=True)
    ink, best = min(tries)
    if ink > TITLE_BAND_INK_MAX:
        print("thumbnail: warning: the picture still reaches into the title band; the title's white halo"
              " keeps it readable, or redraw with `just thumbnail <id> --redraw`", file=sys.stderr)
    final = outdir / f"thumbnail-art{best.suffix}"
    best.rename(final)
    for _, p in tries:
        if p != best:
            p.unlink(missing_ok=True)
    return final


def fit_title(title: str, draw, path: Path):
    """(font, lines) for the largest size at which the title fits the band in at most MAX_LINES
    lines, with the words spread evenly over the lines."""
    from PIL import ImageFont

    words = (title.upper() if UPPERCASE else title).split()
    max_w, max_h = W * (1 - 2 * MARGIN), H * (TITLE_BOTTOM - TITLE_TOP)
    for size in range(MAX_SIZE, MIN_SIZE - 1, -4):
        font = ImageFont.truetype(str(path), size)
        if FONT_WEIGHT:
            font.set_variation_by_name(FONT_WEIGHT)
        width = lambda s: draw.textlength(s, font=font) + 2 * STROKE
        for n in range(1, min(MAX_LINES, len(words)) + 1):
            if n * size * LINE_SPACING > max_h:
                break
            best = best_split(words, n, width)
            if best and max(map(width, best)) <= max_w:
                return font, best
    sys.exit(f"title {title!r} doesn't fit the thumbnail even at {MIN_SIZE}px; shorten it")


def best_split(words: list[str], n: int, width) -> list[str] | None:
    """The words in n lines, splitting where the widest line is narrowest."""
    from itertools import combinations

    best, best_w = None, float("inf")
    for cuts in combinations(range(1, len(words)), n - 1):
        bounds = (0, *cuts, len(words))
        lines = [" ".join(words[a:b]) for a, b in zip(bounds, bounds[1:])]
        w = max(map(width, lines))
        if w < best_w:
            best, best_w = lines, w
    return best


def set_title(art: Path, title: str, dest: Path) -> Path:
    """The picture at W x H with the title centred in the title band."""
    from PIL import Image, ImageDraw

    im = Image.open(art).convert("RGB").resize((W, H), Image.LANCZOS)
    draw = ImageDraw.Draw(im)
    font, lines = fit_title(title, draw, font_path())
    step = font.size * LINE_SPACING
    y = H * (TITLE_TOP + TITLE_BOTTOM) / 2 - step * len(lines) / 2 + (step - font.size) / 2
    for line in lines:
        draw.text((W / 2, y), line, font=font, fill=TEXT_COLOUR, anchor="mt",
                  stroke_width=STROKE, stroke_fill=STROKE_COLOUR)
        y += step
    im.save(dest)
    return dest


def make(nid: str, title: str, lines: list[str], board: dict | None, redraw: bool = False,
         dry_run: bool = False) -> Path | None:
    """Draw the picture if there isn't one (or redraw), then set the title. Returns the thumbnail."""
    outdir = OUT / nid
    outdir.mkdir(parents=True, exist_ok=True)
    prompt = art_prompt(title, lines, board)
    art = find_art(outdir)
    if dry_run:
        (outdir / "thumbnail-art.txt").write_text(prompt)
        print(f"\n===== thumbnail-art =====\n{prompt}")
        return None
    if art and redraw:
        hist = outdir / "history"
        hist.mkdir(exist_ok=True)
        k = 1
        while (hist / f"thumbnail-art.v{k}{art.suffix}").exists():
            k += 1
        shutil.move(art, hist / f"thumbnail-art.v{k}{art.suffix}")
        art = None
    if not art:
        art = draw_art(outdir, prompt)
    dest = set_title(art, title, OUT / f"{nid}-thumbnail.png")
    print(f"thumbnail: {dest}")
    return dest


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("note", help="note ID, e.g. 001")
    ap.add_argument("--redraw", action="store_true",
                    help=f"draw a new picture (~${IMAGE_COST:.3f}); the old one goes to history/")
    ap.add_argument("--dry-run", action="store_true", help="write and print the prompt, no API calls")
    args = ap.parse_args()
    title, lines = read_note(args.note)
    make(args.note, title, lines, load_board(OUT / args.note, lines), args.redraw, args.dry_run)


if __name__ == "__main__":
    run_cli(main)
