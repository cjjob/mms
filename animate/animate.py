#!/usr/bin/env python3
"""Turn one note into a sequence of stick-figure frames, one per Description line.

Usage:
  python3 animate/animate.py 001             # generate frames for notes/001.txt
  python3 animate/animate.py 001 --dry-run   # write/print prompts only, no API calls
  python3 animate/animate.py 001 --storyboard-only [--replan]
                                             # plan scenes and print them, no images

Steps:
  1. Storyboard: a cheap text model reads the whole script once and writes a
     cast description plus one concrete, drawable scene per line.
  2. Frames: for each line, the image model gets the style reference, the
     previous frame(s), the whole script with the current line marked, and
     that line's scene.

Output goes to animate/out/<id>/:
  storyboard.json   the shot plan (reused on later runs; --replan to redo it)
  frame-NN.png      one image per line
  frame-NN.txt      the exact prompt used for that frame
Existing frames are skipped. To change individual frames afterwards, use edit.py
(`just edit` / `just frame`).

Needs: pip install -r requirements.txt, and GOOGLE_API_KEY in .env.
"""

import argparse
import base64
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from build import FIELD_BY_KEY, FIELD_RE, SRC  # reuse the note format  # noqa: E402

PLAN_MODEL = "gemini-3.1-flash-lite"         # text, used once per note
IMAGE_MODEL = "gemini-3.1-flash-lite-image"  # cheapest image model, ~$0.034 per image
IMAGE_COST = 0.0336
REFERENCE = HERE / "animate_reference.png"
PREV_FRAMES = 2  # how many earlier frames to feed back in for continuity
OUT = HERE / "out"

STYLE = (
    "Draw in exactly the style of the reference image: a hand-drawn stick figure "
    "with a round head, a simple expressive face, rounded hands and feet, and clean, "
    "even, black outlines on a pure white background. Black and white only: no colour, "
    "no grey shading, no gradients. Any props or setting are minimal and drawn in the "
    "same line style. No text, letters, numbers, captions or speech bubbles anywhere. "
    "COMPOSITION (important): vertical 9:16 frame. Place the main subject and action "
    "in the centre of the frame, within the middle half of the height. Keep the lower "
    "third (from about 65% of the way down to the bottom) plain white or only "
    "unimportant ground/floor lines, because captions are overlaid there. Keep the top "
    "eighth empty too (app UI). Nothing important near the edges."
)


def load_env() -> None:
    """Minimal .env loader so we don't need python-dotenv. Values in .env override the shell."""
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.strip().startswith("#"):
            os.environ[key.strip()] = value.strip().strip("\"'")  # .env wins over the shell


def clean(text: str) -> str:
    """Drop markdown emphasis markers; the model only needs the words."""
    return re.sub(r"[*_]", "", text).strip()


def read_note(nid: str) -> tuple[str, list[str]]:
    """Return (title, description lines). One non-empty line = one frame."""
    path = SRC / f"{nid}.txt"
    if not path.exists():
        sys.exit(f"no such note: {path}")
    fields: dict[str, list[str]] = {}
    current = None
    for raw in path.read_text().splitlines():
        if raw.lstrip().startswith("#"):
            continue
        m = FIELD_RE.match(raw.strip())
        if m:
            current = FIELD_BY_KEY[m.group(1).lower()]
            fields.setdefault(current, [])
            if m.group(2).strip():
                fields[current].append(m.group(2).strip())
            continue
        if current and raw.strip():
            fields[current].append(raw.strip())
    title = clean(" ".join(fields.get("Title", [])))
    lines = [clean(line) for line in fields.get("Description", [])]
    if not lines:
        sys.exit(f"{path.name}: Description is empty")
    return title, lines


def numbered(lines: list[str], mark: int | None = None) -> str:
    out = []
    for i, line in enumerate(lines, 1):
        out.append(f">>> {i}. {line} <<<  (THIS FRAME)" if i == mark else f"{i}. {line}")
    return "\n".join(out)


def make_storyboard(client, title: str, lines: list[str]) -> dict:
    prompt = f"""You are storyboarding a short narrated video drawn as simple black-and-white
stick-figure cartoons. Each numbered narration line below gets exactly one frame.

Return ONLY JSON, no commentary, in this shape:
{{"cast": "...", "frames": [{{"n": 1, "scene": "..."}}, ...]}}

Rules:
- "cast": the recurring characters (at most 3), described precisely enough to draw them
  identically every time. The main character is the plain stick figure from the
  reference image; tell other characters apart by a single simple feature (e.g. a cap,
  a ponytail, taller), never by colour.
- "frames": exactly {len(lines)} items, in order, one per line.
- "scene": one or two sentences describing a concrete, drawable moment: who, doing
  what, with which simple props or setting. Turn abstract lines into a clear visual
  metaphor. Consecutive frames should flow like one continuous story with the same
  characters, not unrelated pictures.
- No text, words or speech bubbles in any scene.

Title: {title}

Narration:
{numbered(lines)}"""
    r = client.interactions.create(model=PLAN_MODEL, input=prompt)
    text = r.output_text or ""
    try:
        board = json.loads(text[text.index("{") : text.rindex("}") + 1])
        scenes = [f["scene"] for f in board["frames"]]
        assert len(scenes) == len(lines)
    except (ValueError, KeyError, TypeError, AssertionError):
        print("warning: storyboard unusable, falling back to raw lines", file=sys.stderr)
        board = {"cast": "The stick figure from the reference image.", "frames": []}
        scenes = list(lines)
    return {"cast": board.get("cast", ""), "scenes": scenes}


def frame_prompt(i: int, lines: list[str], board: dict, n_prev: int, n_next: int = 0) -> str:
    """Prompt for drawing frame i. Images sent with it: reference, n_prev earlier frames
    (oldest first), then n_next later frames."""
    n = len(lines)
    images = "Image 1 is the style and main-character reference."
    if n_prev:
        images += (f" Images 2-{n_prev + 1} are the frames that come just before this one, oldest first;"
                   " continue naturally from the last of them.")
    if n_next:
        k = n_prev + 2
        images += f" Image {k}{'-' + str(k + n_next - 1) if n_next > 1 else ''} comes just after this one; lead into it."
    if n_prev or n_next:
        images += (" Keep the same characters, proportions and setting where it makes sense, so the"
                   " frames play as one continuous animation. Do not copy a neighbouring frame; show"
                   " this frame's own moment.")
    else:
        images += " This is the opening frame."
    before = f'Previous line: "{lines[i - 2]}"\n' if i > 1 else ""
    after = f'Next line: "{lines[i]}"\n' if i < n else ""
    return f"""You are drawing frame {i} of {n} of a short narrated stick-figure video,
one frame per narration line.

STYLE: {STYLE}

CAST: {board['cast']}

IMAGES: {images}

FULL SCRIPT (context only; draw just the marked line):
{numbered(lines, mark=i)}

{before}THIS LINE: "{lines[i - 1]}"
{after}
SCENE TO DRAW: {board['scenes'][i - 1]}"""


IMAGE_EXTS = (".png", ".jpg", ".jpeg")


def find_frame(outdir: Path, i: int) -> Path | None:
    for ext in IMAGE_EXTS:
        p = outdir / f"frame-{i:02d}{ext}"
        if p.exists():
            return p
    return None


def load_board(outdir: Path) -> dict | None:
    p = outdir / "storyboard.json"
    return json.loads(p.read_text()) if p.exists() else None


def save_board(outdir: Path, board: dict) -> None:
    (outdir / "storyboard.json").write_text(json.dumps(board, indent=2))


def generate_image(prompt: str, refs: list[Path], outdir: Path, stem: str) -> Path:
    """One image-model call: prompt + reference images in order. Saves outdir/stem.<ext>."""
    r = get_client().interactions.create(
        model=IMAGE_MODEL,
        input=[{"type": "text", "text": prompt}, *map(image_part, refs)],
        response_format={"type": "image", "aspect_ratio": "9:16", "image_size": "1K"},
    )
    img = r.output_image
    if img is None:
        sys.exit(f"{stem}: no image returned. Model said: {r.output_text!r}")
    ext = ".jpg" if "jpeg" in (getattr(img, "mime_type", "") or "") else ".png"
    path = outdir / f"{stem}{ext}"
    path.write_bytes(base64.b64decode(img.data))
    return path


_client = None


def get_client():
    """Create the Gemini client on first use, so previews of saved work need no key."""
    global _client
    if _client is None:
        load_env()
        key = os.environ.get("GOOGLE_API_KEY")
        if not key:
            sys.exit("GOOGLE_API_KEY missing (put it in .env)")
        from google import genai
        _client = genai.Client(api_key=key)
    return _client


def image_part(path: Path) -> dict:
    mime = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    return {"type": "image", "mime_type": mime,
            "data": base64.b64encode(path.read_bytes()).decode("utf-8")}


def print_storyboard(board: dict, lines: list[str], path: Path) -> None:
    print(f"\nCAST: {board['cast']}\n")
    for i, (line, scene) in enumerate(zip(lines, board["scenes"]), 1):
        print(f"{i:02d}. {line}\n    -> {scene}\n")
    print(f"saved: {path}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("note", help="note ID, e.g. 001")
    ap.add_argument("--dry-run", action="store_true", help="write and print prompts, no API calls")
    ap.add_argument("--storyboard-only", action="store_true",
                    help="generate (or reuse) the storyboard, print it, and stop before images")
    ap.add_argument("--replan", action="store_true", help="regenerate storyboard.json even if it exists")
    args = ap.parse_args()

    title, lines = read_note(args.note)
    outdir = OUT / args.note
    outdir.mkdir(parents=True, exist_ok=True)
    print(f"{args.note}: {title!r}, {len(lines)} frames (~${len(lines) * IMAGE_COST:.2f} in images)")
    if args.dry_run and (args.storyboard_only or args.replan):
        sys.exit("--dry-run can't be combined with --storyboard-only/--replan")


    board_path = outdir / "storyboard.json"
    if board_path.exists() and not args.replan:
        board = load_board(outdir)
    elif args.dry_run:
        board = {"cast": "(storyboard is generated on a real run)", "scenes": list(lines)}
    else:
        print("storyboarding...")
        board = make_storyboard(get_client(), title, lines)
        save_board(outdir, board)

    if args.storyboard_only:
        print_storyboard(board, lines, board_path)
        return

    prev: list[Path] = []
    for i in range(1, len(lines) + 1):
        stem = f"frame-{i:02d}"
        existing = find_frame(outdir, i)
        if existing:
            print(f"{stem}: exists, skipping")
            prev.append(existing)
            continue

        refs = prev[-PREV_FRAMES:]
        prompt = frame_prompt(i, lines, board, len(refs))
        (outdir / f"{stem}.txt").write_text(prompt)
        if args.dry_run:
            print(f"\n===== {stem} (+ reference + {len(refs)} previous) =====\n{prompt}")
            continue

        print(f"{stem}: {lines[i - 1][:60]}")
        prev.append(generate_image(prompt, [REFERENCE, *refs], outdir, stem))

    print(f"done: {outdir}")


def run_cli(fn) -> None:
    """Run fn, turning API errors into a one-line message instead of the SDK's traceback."""
    try:
        fn()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        if type(e).__module__.startswith("google."):
            sys.exit(f"Gemini API error: {e}")
        raise


if __name__ == "__main__":
    run_cli(main)
