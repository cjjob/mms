#!/usr/bin/env python3
"""Turn one note into a sequence of stick-figure frames, as planned by the storyboard.

Usage:
  python3 animate/animate.py 001             # generate frames for notes/001.txt
  python3 animate/animate.py 001 --dry-run   # write/print prompts only, no API calls
  python3 animate/animate.py 001 --storyboard-only [--replan]
                                             # plan scenes and print them, no images

Steps:
  1. Storyboard: a cheap text model reads the whole script (every word numbered)
     and decides how many frames there are and where each starts (on any word).
     Each frame gets one concrete, drawable scene and a camera move (see stitch.py).
  2. Frames: for each frame, the image model gets the style reference, the
     previous frame(s), the whole script with this frame's words marked, and
     its scene.

Output goes to animate/out/<id>/:
  storyboard.json   the shot plan: cast + frames [{start word, text, scene, camera}]
                    (reused on later runs; --replan to redo it)
  frame-NN.png      one image per storyboard frame
  frame-NN.txt      the exact prompt used for that frame
Existing frames are skipped. To change individual frames afterwards, use edit.py
(`just edit` / `just frame`).

Needs: pip install -r requirements.txt, and GOOGLE_API_KEY in .env.

Images: set IMAGE_BACKEND=local (in .env or the shell) to draw frames on this
Mac with FLUX.2 klein 4B via mflux instead of Gemini. One-time setup:
`just setup-local`. Text (storyboard, edit planning) always uses Gemini.
"""

import argparse
import base64
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from build import FIELD_BY_KEY, FIELD_RE, SRC, load_config  # reuse the note format  # noqa: E402

TEXT_MODEL = load_config()["text_model"]     # text: storyboard and edits; set in config.toml
IMAGE_MODEL = "gemini-3.1-flash-lite-image"  # cheapest image model, ~$0.034 per image
IMAGE_COST = 0.0336
REFERENCE = HERE / "animate_reference.png"
PREV_FRAMES = 4  # how many earlier frames to feed back in for continuity
REQUEST_TIMEOUT = 240  # seconds per Gemini call; normal is 30-100s, without this a stalled call hangs forever
OUT = HERE / "out"

PROMPTS = HERE / "prompts"  # one editable text file per prompt; see prompts/README.md


def load_prompt(name: str, **values) -> str:
    """Read prompts/<name>.txt and fill in its {placeholders}. Braces that aren't a placeholder
    (e.g. the JSON example in storyboard.txt) are left alone."""
    text = (PROMPTS / f"{name}.txt").read_text().rstrip("\n")
    unknown = sorted(set(re.findall(r"\{(\w+)\}", text)) - set(values))
    if unknown:
        print(f"warning: prompts/{name}.txt has unknown placeholders {unknown}; sent as-is", file=sys.stderr)
    return re.sub(r"\{(\w+)\}", lambda m: str(values.get(m[1], m[0])), text)


STYLE = load_prompt("style")


# ---- local image backend (IMAGE_BACKEND=local) ----
LOCAL_IMAGE_MODEL = HERE / "models" / "flux2-klein-4b-q4"  # made by `just setup-local`
LOCAL_W, LOCAL_H = 576, 1024  # 9:16, multiples of 16; upscaled to 1080x1920 by stitch
LOCAL_STEPS = 4               # klein is distilled for 4 steps
# Small local models follow short, concrete prompts best. This describes the look;
# swap animate_reference.png for a drawing of your own character to set it.
LOCAL_STYLE = load_prompt("style_local")


def backend() -> str:
    """Image backend: IMAGE_BACKEND, google (default) or local."""
    load_env()
    return env_choice("IMAGE_BACKEND")


def image_cost() -> float:
    return 0.0 if backend() == "local" else IMAGE_COST


def ask_text(prompt: str) -> str:
    """One text-model call (storyboard, edit planning). Always Gemini: see the README."""
    return get_client().interactions.create(model=TEXT_MODEL, input=prompt).output_text or ""


def local_frame_prompt(i: int, board: dict, n_refs: int) -> str:
    cont = " Continue the story from image 2: same characters, same proportions." if n_refs > 1 else ""
    return load_prompt("frame_local", style=LOCAL_STYLE, continue_from_previous=cont, cast=board["cast"],
                       setting=setting_line(board, i), scene=board["frames"][i - 1]["scene"])


def setting_line(board: dict, i: int) -> str:
    st = setting_of(board, i)
    return f"Setting: {st['look']} Always in the background: {st['background']} " if st else ""


def local_edit_prompt(instruction: str) -> str:
    return load_prompt("edit_local", instruction=instruction, style=LOCAL_STYLE)


_local_model = None


def local_image(prompt: str, refs: list[Path], path: Path) -> None:
    global _local_model
    try:
        from mflux.models.common.config import ModelConfig
        from mflux.models.flux2.variants import Flux2KleinEdit
    except ImportError:
        sys.exit("mflux not installed: just setup-local")
    if _local_model is None:
        print("loading FLUX.2 klein 4B...")
        if LOCAL_IMAGE_MODEL.exists():
            kw = {"model_path": str(LOCAL_IMAGE_MODEL)}
        else:  # not saved yet: download (~15GB, once) and quantize on every load
            print(f"  ({LOCAL_IMAGE_MODEL} not found, quantizing on load; `just setup-local` makes this faster)")
            kw = {"quantize": 4}
        _local_model = Flux2KleinEdit(model_config=ModelConfig.flux2_klein_4b(), **kw)
    img = _local_model.generate_image(
        seed=int.from_bytes(os.urandom(4), "big"),
        prompt=prompt,
        image_paths=[str(r) for r in refs],
        num_inference_steps=LOCAL_STEPS,
        width=LOCAL_W,
        height=LOCAL_H,
    )
    img.save(str(path), overwrite=True)


# Settings with a fixed set of values (the first is the default), checked whenever .env is loaded
CHOICES = {
    "IMAGE_BACKEND": ("google", "local"),
    "VOICE_BACKEND": ("google", "elevenlabs"),
}


def env_choice(key: str) -> str:
    """The value of a CHOICES setting from the environment, or its default; exits if it's not allowed."""
    allowed = CHOICES[key]
    value = os.environ.get(key, "").strip().lower() or allowed[0]
    if value not in allowed:
        sys.exit(f"{key}={value!r} isn't valid (in .env or the shell); use one of: {', '.join(allowed)}")
    return value


def load_env() -> None:
    """Minimal .env loader so we don't need python-dotenv. Values in .env override the shell.
    Then checks every CHOICES setting, so a typo'd backend fails straight away."""
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and not key.strip().startswith("#"):
                os.environ[key.strip()] = value.strip().strip("\"'")  # .env wins over the shell
    for key in CHOICES:
        env_choice(key)


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


WORDS_PER_SECOND = 2.5  # rough narration speed, for estimates
MAX_FRAMES = 120        # safety limit, not a target
CAMERA_MOVES = ("hold", "zoom_in", "zoom_out", "pan")  # applied by stitch.py


def script_words(lines: list[str]) -> list[str]:
    """The whole narration as one word list. Frames point into it by index."""
    return [w for line in lines for w in line.split()]


def script_text(lines: list[str]) -> str:
    """The narration as voiced: every line, in one go."""
    return " ".join(lines)


def script_timings(words: list[str], timed: list[list]) -> list[list]:
    """[word, start, end] for each of `words`, from timed tokens ([text, start, end]) that may be
    split differently (stable-ts splits some words; ElevenLabs times single characters).
    Matched by their letters/digits, so punctuation and spacing differences don't matter."""
    norm = lambda t: re.sub(r"\W", "", t.lower())
    starts, ends = [], []  # one entry per letter/digit across the timed tokens
    for w, s, e in timed:
        n = len(norm(w))
        starts += [s + (e - s) * k / n for k in range(n)]
        ends += [s + (e - s) * (k + 1) / n for k in range(n)]
    total = sum(len(norm(w)) for w in words) or 1
    out, pos = [], 0
    for w in words:
        n = len(norm(w))
        if not starts:
            out.append([w, 0.0, 0.0])
            continue
        a = min(len(starts) - 1, round(pos * len(starts) / total))
        b = min(len(starts) - 1, max(a, round((pos + n) * len(starts) / total) - 1))
        out.append([w, round(starts[a], 3), round(ends[b], 3)])
        pos += n
    return out


def line_starts(lines: list[str]) -> list[int]:
    """Index (into script_words) of each line's first word."""
    out, k = [], 0
    for line in lines:
        out.append(k)
        k += len(line.split())
    return out


def numbered(lines: list[str]) -> str:
    """Narration with every word numbered, one note line per row."""
    rows, k = [], 0
    for line in lines:
        ws = line.split()
        rows.append(" ".join(f"[{k + j}]{w}" for j, w in enumerate(ws)))
        k += len(ws)
    return "\n".join(rows)


def spans(board: dict, lines: list[str]) -> list[tuple[int, int]]:
    """(start, end) word indices for each frame; a frame runs until the next one starts."""
    starts = [f["start"] for f in board["frames"]]
    return list(zip(starts, starts[1:] + [len(script_words(lines))]))


def frame_text(board: dict, lines: list[str], i: int) -> str:
    s, e = spans(board, lines)[i - 1]
    return " ".join(script_words(lines)[s:e])


def clean_frames(raw: list, words: list[str]) -> list[dict]:
    """Validate the model's frames: valid, strictly increasing start indices, first at 0.
    Off-by-a-few indices are repaired using the quoted text."""
    norm = lambda w: re.sub(r"\W", "", w.lower())
    n, out, last = len(words), [], -1
    for f in raw if isinstance(raw, list) else []:
        if not isinstance(f, dict) or not str(f.get("scene") or "").strip():
            continue
        try:
            s = int(f.get("start"))
        except (TypeError, ValueError):
            continue
        quote = [norm(w) for w in str(f.get("text") or "").split()[:3]]
        if quote and [norm(w) for w in words[s:s + len(quote)]] != quote:
            for c in sorted(range(max(0, s - 15), min(n, s + 16)), key=lambda c: abs(c - s)):
                if [norm(w) for w in words[c:c + len(quote)]] == quote:
                    s = c
                    break
        if not out:
            s = 0
        if s <= last or s >= n:
            continue
        frame = {"start": s, "setting": str(f.get("setting") or "").strip(), "scene": str(f["scene"]).strip()}
        cam = clean_camera(f.get("camera"))
        if cam:
            frame["camera"] = cam
        out.append(frame)
        last = s
    return out[:MAX_FRAMES]


def clean_camera(raw) -> dict | None:
    """Validate a frame's camera: {"move", "focus": {"x", "y"} (fractions, clamped to 0-1),
    "intensity" (optional, 0-1)}. None if the move isn't recognised (stitch then uses its default)."""
    if not isinstance(raw, dict):
        return None
    move = re.sub(r"[\s-]+", "_", str(raw.get("move") or "").strip().lower())
    if move not in CAMERA_MOVES:
        return None
    focus = raw.get("focus")
    if isinstance(focus, (list, tuple)) and len(focus) == 2:
        focus = {"x": focus[0], "y": focus[1]}
    focus = focus if isinstance(focus, dict) else {}

    def frac(v, default):
        try:
            return round(max(0.0, min(1.0, float(v))), 3)
        except (TypeError, ValueError):
            return default

    cam = {"move": move, "focus": {"x": frac(focus.get("x"), 0.5), "y": frac(focus.get("y"), 0.5)}}
    if raw.get("intensity") is not None:
        cam["intensity"] = frac(raw["intensity"], 0.5)
    return cam


def clean_settings(raw) -> list[dict]:
    out = []
    for st in raw if isinstance(raw, list) else []:
        if isinstance(st, dict) and str(st.get("name") or "").strip():
            out.append({k: str(st.get(k) or "").strip() for k in ("name", "look", "background")})
    return out


def fix_frame_settings(frames: list[dict], settings: list[dict]) -> None:
    """Every frame names a known setting; an unknown one inherits the previous frame's."""
    names = [st["name"] for st in settings]
    last = names[0] if names else ""
    for f in frames:
        if f.get("setting") not in names:
            f["setting"] = last
        last = f["setting"]


def setting_of(board: dict, i: int) -> dict | None:
    name = board["frames"][i - 1].get("setting")
    return next((st for st in board.get("settings", []) if st["name"] == name), None)


def setting_block(board: dict, i: int) -> str:
    """The SETTING section of a frame prompt ("" for old storyboards without settings)."""
    st = setting_of(board, i)
    if not st:
        return ""
    same = i > 1 and board["frames"][i - 2].get("setting") == st["name"]
    cont = ("Same place as the previous frame: keep its layout and these background details exactly"
            " where they were." if same else "The story arrives here in this frame: show the place clearly.")
    return (f"SETTING ({st['name']}): {st['look']}\n"
            f"BACKGROUND DETAILS (always in the frame, small, same place every time): {st['background']}\n"
            f"{cont}\n\n")


def establishing_frame(outdir: Path, board: dict, i: int, skip: list[Path]) -> Path | None:
    """The first drawn frame in frame i's setting, if it isn't already among the refs: shows the
    image model what this place looked like, so the background stays the same."""
    st = setting_of(board, i)
    if not st:
        return None
    for k in range(1, i):
        if board["frames"][k - 1].get("setting") == st["name"]:
            p = find_frame(outdir, k)
            return p if p and p not in skip else None
    return None


def with_text(board: dict, lines: list[str]) -> dict:
    """Refresh each frame's "text" (the words it covers), for readability in storyboard.json."""
    for i, f in enumerate(board["frames"], 1):
        f["text"] = frame_text(board, lines, i)
    return board


def make_storyboard(title: str, lines: list[str]) -> dict:
    words = script_words(lines)
    prompt = load_prompt(
        "storyboard", last_word=len(words) - 1, title=title, n_words=len(words), seconds=f"{len(words) / WORDS_PER_SECOND:.0f}",
        script=numbered(lines))
    text = ask_text(prompt)
    try:
        board = json.loads(text[text.index("{") : text.rindex("}") + 1])
        frames = clean_frames(board.get("frames"), words)
        assert frames
        settings = clean_settings(board.get("settings"))
        fix_frame_settings(frames, settings)
    except (ValueError, KeyError, TypeError, AttributeError, AssertionError):
        print("warning: storyboard unusable, falling back to one frame per line", file=sys.stderr)
        board = {"cast": "The stick figure from the reference image."}
        settings = []
        frames = [{"start": s, "scene": line} for s, line in zip(line_starts(lines), lines)]
    return with_text({"cast": board.get("cast", ""), "settings": settings, "frames": frames}, lines)


def marked_script(board: dict, lines: list[str], i: int) -> str:
    s, e = spans(board, lines)[i - 1]
    w = script_words(lines)
    return " ".join([*w[:s], ">>>", *w[s:e], "<<<", *w[e:]])


def frame_prompt(i: int, lines: list[str], board: dict, n_prev: int, n_next: int = 0,
                 n_setting: int = 0) -> str:
    """Prompt for drawing frame i. Images sent with it: reference, n_prev earlier frames
    (oldest first), then n_next later frames, then (n_setting=1) the first frame drawn in
    this frame's setting."""
    n = len(board["frames"])
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
    if n_setting:
        images += (f" Image {n_prev + n_next + 2} is an earlier frame in this same place: match its"
                   " setting and background details exactly (positions, sizes, shapes).")
    before = f'Previous frame\'s words: "{frame_text(board, lines, i - 1)}"\n' if i > 1 else ""
    after = f'Next frame\'s words: "{frame_text(board, lines, i + 1)}"\n' if i < n else ""
    return load_prompt(
        "frame", frame=i, n_frames=n, style=STYLE, cast=board["cast"], setting=setting_block(board, i),
        images=images, script=marked_script(board, lines, i), previous_words=before,
        words=frame_text(board, lines, i), next_words=after, scene=board["frames"][i - 1]["scene"])


IMAGE_EXTS = (".png", ".jpg", ".jpeg")


def find_frame(outdir: Path, i: int) -> Path | None:
    for ext in IMAGE_EXTS:
        p = outdir / f"frame-{i:02d}{ext}"
        if p.exists():
            return p
    return None


def load_board(outdir: Path, lines: list[str]) -> dict | None:
    p = outdir / "storyboard.json"
    if not p.exists():
        return None
    board = json.loads(p.read_text())
    if "scenes" in board and "frames" not in board:  # old one-frame-per-line format
        board["frames"] = [{"start": s, "scene": sc} for s, sc in zip(line_starts(lines), board.pop("scenes"))]
    return with_text(board, lines)


def save_board(outdir: Path, board: dict) -> None:
    board = {"cast": board["cast"], "settings": board.get("settings", []), "frames": board["frames"],
             **{k: v for k, v in board.items() if k not in ("cast", "settings", "frames")}}
    (outdir / "storyboard.json").write_text(json.dumps(board, indent=2))


def generate_image(prompt: str, refs: list[Path], outdir: Path, stem: str,
                   local_prompt: str | None = None, local_refs: list[Path] | None = None) -> Path:
    """One image-model call: prompt + reference images in order. Saves outdir/stem.<ext>.
    The local backend uses local_prompt/local_refs (shorter, fewer images) when given."""
    if backend() == "local":
        path = outdir / f"{stem}.png"
        local_image(local_prompt or prompt, local_refs if local_refs is not None else refs[:2], path)
        return path
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
        if os.environ.get("DEBUG"):  # DEBUG=1 just animate 001: log every HTTP request/response
            import logging
            logging.basicConfig(level=logging.DEBUG, format="%(asctime)s %(name)s: %(message)s",
                                datefmt="%H:%M:%S")
        from google import genai
        _client = genai.Client(api_key=key,
                               http_options=genai.types.HttpOptions(timeout=REQUEST_TIMEOUT * 1000))
    return _client


def image_part(path: Path) -> dict:
    mime = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    return {"type": "image", "mime_type": mime,
            "data": base64.b64encode(path.read_bytes()).decode("utf-8")}


def print_storyboard(board: dict, lines: list[str], path: Path) -> None:
    print(f"\nCAST: {board['cast']}\n")
    for st in board.get("settings", []):
        print(f"SETTING {st['name']}: {st['look']}\n    background: {st['background']}\n")
    for i, ((s, e), f) in enumerate(zip(spans(board, lines), board["frames"]), 1):
        where = f"  [{f['setting']}]" if f.get("setting") else ""
        cam = f.get("camera")
        cam = (f"  {cam['move']} @ ({cam['focus']['x']:g}, {cam['focus']['y']:g})"
               + (f" x{cam['intensity']:g}" if "intensity" in cam else "")) if cam else ""
        print(f"{i:02d}. ~{(e - s) / WORDS_PER_SECOND:.1f}s{where}{cam}  \"{f['text']}\"\n    -> {f['scene']}\n")
    n = len(board["frames"])
    cost = f", ~${n * image_cost():.2f} in images" if image_cost() else ""
    print(f"{n} frames{cost}. saved: {path}")


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
    print(f"{args.note}: {title!r}, {len(script_words(lines))} words")
    if args.dry_run and (args.storyboard_only or args.replan):
        sys.exit("--dry-run can't be combined with --storyboard-only/--replan")

    board_path = outdir / "storyboard.json"
    if board_path.exists() and not args.replan:
        board = load_board(outdir, lines)
    elif args.dry_run:
        sys.exit(f"no storyboard yet; run `just storyboard {args.note}` first (it's ~free)")
    else:
        stale = sorted(p for p in outdir.glob("frame-*") if p.is_file())
        if stale:  # images from the old plan don't match the new one
            k = 1
            while (outdir / "history" / f"before-replan-{k}").exists():
                k += 1
            dest = outdir / "history" / f"before-replan-{k}"
            dest.mkdir(parents=True)
            for p in stale:
                p.rename(dest / p.name)
            if board_path.exists():
                board_path.rename(dest / board_path.name)
            print(f"moved the old plan's {len(stale)} frame files to {dest}")
        print("storyboarding...")
        board = make_storyboard(title, lines)
        save_board(outdir, board)

    if args.storyboard_only:
        print_storyboard(board, lines, board_path)
        return

    n = len(board["frames"])
    todo = sum(1 for i in range(1, n + 1) if not find_frame(outdir, i))
    where = f"~${todo * image_cost():.2f}" if image_cost() else "local"
    print(f"{n} frames, {todo} to generate ({where})")

    prev: list[Path] = []
    for i in range(1, n + 1):
        stem = f"frame-{i:02d}"
        existing = find_frame(outdir, i)
        if existing:
            print(f"{stem}: exists, skipping")
            prev.append(existing)
            continue

        refs = prev[-PREV_FRAMES:]
        est = [p for p in [establishing_frame(outdir, board, i, refs)] if p]
        prompt = frame_prompt(i, lines, board, len(refs), 0, len(est))
        local_refs = [REFERENCE, *prev[-1:]]  # local: reference + previous frame only
        local_prompt = local_frame_prompt(i, board, len(local_refs))
        used = local_prompt if backend() == "local" else prompt
        (outdir / f"{stem}.txt").write_text(used)
        if args.dry_run:
            print(f"\n===== {stem} (+ reference + {len(refs)} previous) =====\n{used}")
            continue

        t0 = time.monotonic()
        print(f"{time.strftime('%H:%M:%S')} {stem}: {frame_text(board, lines, i)[:60]}", flush=True)
        prev.append(generate_image(prompt, [REFERENCE, *refs, *est], outdir, stem, local_prompt, local_refs))
        print(f"           done in {time.monotonic() - t0:.0f}s", flush=True)

    print(f"done: {outdir}")


LOGS = ROOT / "logs"


class _Tee:
    """Pass writes through to a terminal stream and copy them, time-stamped per line, to a log file."""

    def __init__(self, stream, log):
        self._stream, self._log, self._at_line_start = stream, log, True

    def write(self, text: str) -> int:
        self._stream.write(text)
        for part in text.splitlines(keepends=True):
            if self._at_line_start:
                self._log.write(time.strftime("%H:%M:%S "))
            self._log.write(part)
            self._at_line_start = part.endswith("\n")
        self._log.flush()
        return len(text)

    def flush(self) -> None:
        self._stream.flush()
        self._log.flush()

    def __getattr__(self, name):  # isatty, fileno, encoding, ... from the real stream
        return getattr(self._stream, name)


def start_log() -> Path:
    """Copy this run's stdout/stderr to logs/<date>_<time>_<script>_<args>.log."""
    LOGS.mkdir(exist_ok=True)
    script = Path(sys.argv[0]).stem
    args = re.sub(r"[^\w.-]+", "-", "_".join(sys.argv[1:]))[:60].strip("-_")
    path = LOGS / f"{time.strftime('%Y-%m-%d_%H%M%S')}_{script}{'_' + args if args else ''}.log"
    log = path.open("a", encoding="utf-8")
    log.write(f"{time.strftime('%H:%M:%S')} $ {' '.join([script, *sys.argv[1:]])}\n")
    sys.stdout, sys.stderr = _Tee(sys.stdout, log), _Tee(sys.stderr, log)
    return path


def run_cli(fn) -> None:
    """Run fn with its output logged to logs/, turning API errors into a one-line message
    instead of the SDK's traceback."""
    start_log()
    load_env()  # every script checks .env up front
    try:
        fn()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        if type(e).__name__ in ("TimeoutException", "ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout"):
            sys.exit(f"Gemini API call timed out after {REQUEST_TIMEOUT}s ({type(e).__name__}). "
                     "Rerun to continue; finished frames are kept.")
        if type(e).__module__.startswith("google."):
            sys.exit(f"Gemini API error: {e}")
        raise


if __name__ == "__main__":
    run_cli(main)
