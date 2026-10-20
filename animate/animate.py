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
     A build frame ("build": true) is the previous frame plus one new element, like the
     next bullet point on a slide: drawn as an addition to the previous frame, then
     locked to it everywhere outside the addition (buildup.py).

Output goes to animate/out/<id>/:
  storyboard.json   the shot plan: cast + frames [{start word, text, scene, camera}]
                    (reused on later runs; --replan to redo it)
  frame-NN.png      one image per storyboard frame
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
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from build import FIELD_BY_KEY, FIELD_RE, SRC, load_config  # reuse the note format  # noqa: E402

CONFIG = load_config()
TEXT_MODEL = CONFIG["text_model"]    # storyboard and edit planning; set in config.toml
IMAGE_MODEL = CONFIG["image_model"]  # draws the frames; set in config.toml
JUDGE_MODEL = CONFIG["judge_model"]  # checks each frame (qa.py); set in config.toml
IMAGE_COST = 0.045  # rough $ per frame incl. reference-image input (see config.toml)
# Earlier frames fed back in. Kept at 1 on purpose: every frame is anchored to the fixed references
# and setting image, and the previous frame only hints at poses/props. Chaining several generated
# frames lets drift compound and makes the model copy their layout.
PREV_FRAMES = 1

# Frame layout, as fractions of the 9:16 frame: the one source of truth. The prompts are given
# these numbers, stitch.py puts the captions in the caption band, and qa.py checks them.
# Roughly the TikTok, Reels and Shorts safe zones at 1080x1920. (The strictest ad specs put the
# bottom UI at ~35%, i.e. captions above 65%, but that looked too high in practice.)
SAFE_TOP = 0.15                     # above this: app UI (y < ~288px)
CAPTION_TOP, CAPTION_BOTTOM = 0.62, 0.72  # captions sit here (~y 1190-1380px), the usual spot for
                                          # Reels/TikTok captions, above the account name and buttons
SAFE_LEFT, SAFE_RIGHT = 0.11, 0.82  # x ~120-888px; the like/comment buttons are on the right
MAX_ZOOM = 0.2                      # camera zoom at intensity 1 (stitch.py)
REQUEST_TIMEOUT = 240  # seconds per Gemini call; normal is 30-100s, without this a stalled call hangs forever
OUT = HERE / "out"

PROMPTS = HERE / "prompts"  # one editable text file per prompt; see prompts/README.md


pct = lambda f: f"{f:.0%}"
LAYOUT = {"safe_top_pct": pct(SAFE_TOP), "caption_top_pct": pct(CAPTION_TOP),
          "caption_bottom_pct": pct(CAPTION_BOTTOM), "safe_left_pct": pct(SAFE_LEFT),
          "safe_right_pct": pct(SAFE_RIGHT), "max_zoom_pct": pct(MAX_ZOOM)}
SHARED = ("pipeline", "series", "style")  # shared prompt files, available to every prompt as {name}


def load_prompt(name: str, **values) -> str:
    """Read prompts/<name>.txt and fill in its {placeholders}: the given values, the LAYOUT numbers,
    and the SHARED files ({pipeline}, {series}, {style}), so every model call sees the same brief.
    Braces that aren't a placeholder (e.g. the JSON example in storyboard.txt) are left alone."""
    text = (PROMPTS / f"{name}.txt").read_text().rstrip("\n")
    values = {**LAYOUT, **values}
    for shared in SHARED:
        if shared != name and shared not in values and f"{{{shared}}}" in text:
            values[shared] = load_prompt(shared)
    unknown = sorted(set(re.findall(r"\{(\w+)\}", text)) - set(values))
    if unknown:
        print(f"warning: prompts/{name}.txt has unknown placeholders {unknown}; sent as-is", file=sys.stderr)
    return re.sub(r"\{(\w+)\}", lambda m: str(values.get(m[1], m[0])), text)


STYLE = load_prompt("style")

Images = list[tuple[str, Path]]  # (label, image) pairs, sent in order before the prompt


def references() -> Images:
    """The series reference images from prompts/references.txt ("<name>: <role>" per line), labelled
    "<name>: <role>". The same images, in the same order, go first in every image and check call."""
    out = []
    for line in (PROMPTS / "references.txt").read_text().splitlines():
        name, sep, role = line.partition(":")
        name = name.strip()
        if not sep or not name or line.lstrip().startswith("#"):
            continue
        path = next((p for ext in IMAGE_EXTS if (p := HERE / f"{name}{ext}").exists()), None)
        if not path:
            sys.exit(f"prompts/references.txt names {name}, but there's no animate/{name}.jpg/.png")
        out.append((f"{name} (series reference): {role.strip()}", path))
    if not out:
        sys.exit("prompts/references.txt lists no reference images")
    return out


def labelled(images: Images) -> list[dict]:
    """Interleaved input parts: a text label, then its image, for each image."""
    return [part for i, (label, p) in enumerate(images, 1)
            for part in ({"type": "text", "text": f"[Image {i}] {label}"}, image_part(p))]


def ask_text(prompt: str, images: Images = (), model: str | None = None) -> str:
    """One text-model call (storyboard, edit planning, frame checks): labelled images, then the prompt."""
    inp = [*labelled(list(images)), {"type": "text", "text": prompt}] if images else prompt
    return get_client().interactions.create(model=model or TEXT_MODEL, input=inp).output_text or ""


# Settings with a fixed set of values (the first is the default), checked whenever .env is loaded
CHOICES = {
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
    lines = [clean(spoken(line)) for line in fields.get("Description", [])]
    if not lines:
        sys.exit(f"{path.name}: Description is empty")
    return title, lines


def spoken(line: str) -> str:
    """A list item ("- Nap") as it's voiced and captioned: no marker, and ending in a full stop so
    the voice pauses and the caption breaks after it. Other lines are unchanged."""
    m = re.match(r"[-*+]\s+(.*)", line)
    if not m:
        return line
    item = m.group(1).strip()
    return item if item[-1:] in ".!?…:;," else item + "."


WORDS_PER_SECOND = 2.5  # rough narration speed, for estimates
MAX_FRAMES = 120        # safety limit, not a target
CAMERA_MOVES = ("hold", "zoom_in", "zoom_out", "pan")  # applied by stitch.py
MAX_BUILD_STEPS = 6     # build frames in a row before the plan check complains


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
        if not isinstance(f, dict):
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
        scene, adds = str(f.get("scene") or "").strip(), str(f.get("adds") or "").strip()
        build = str(f.get("build")).strip().lower() == "true" and bool(adds) and bool(out)
        if build and not scene:
            scene = f"{out[-1]['scene']} Now also: {adds}"
        if not scene:
            continue
        frame = {"start": s, "setting": str(f.get("setting") or "").strip(), "scene": scene}
        if build:  # same place as the frame it adds to; the camera is the group's (stitch.py)
            frame.update(build=True, adds=adds, setting=out[-1]["setting"])
        present = f.get("cast")
        if isinstance(present, list):
            frame["cast"] = [str(c).strip() for c in present if str(c).strip()]
        if str(f.get("continuity") or "").strip():
            frame["continuity"] = str(f["continuity"]).strip()
        cam = clean_camera(f.get("camera"))
        if cam and not build:
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

    # The focus is kept inside the main band, where the layout puts everything that matters.
    x = min(SAFE_RIGHT, max(SAFE_LEFT, frac(focus.get("x"), 0.5)))
    y = min(CAPTION_TOP, max(SAFE_TOP, frac(focus.get("y"), (SAFE_TOP + CAPTION_TOP) / 2)))
    cam = {"move": move, "focus": {"x": round(x, 3), "y": round(y, 3)}}
    if raw.get("intensity") is not None:
        cam["intensity"] = frac(raw["intensity"], 0.5)
    return cam


def clean_cast(raw) -> list[dict] | str:
    """[{"name", "look"}]; a plain string (old storyboards) is kept as it is."""
    if isinstance(raw, str):
        return raw.strip()
    out = []
    for c in raw if isinstance(raw, list) else []:
        if isinstance(c, dict) and str(c.get("name") or "").strip():
            out.append({"name": str(c["name"]).strip(), "look": str(c.get("look") or "").strip()})
    return out


def cast_text(board: dict) -> str:
    cast = board.get("cast") or ""
    return cast if isinstance(cast, str) else " ".join(f"{c['name']}: {c['look']}" for c in cast)


def present_text(board: dict, i: int) -> str:
    names = board["frames"][i - 1].get("cast")
    return ", ".join(names) if names else "as the scene describes"


def is_build(board: dict | None, i: int) -> bool:
    """Frame i adds to frame i-1 rather than being a new picture."""
    frames = (board or {}).get("frames") or []
    return 1 < i <= len(frames) and bool(frames[i - 1].get("build"))


def group_of(board: dict | None, i: int) -> tuple[int, int]:
    """(first, last) frame of frame i's build group: a normal frame and the build frames after it.
    (i, i) for a frame on its own. A group shares one camera move (stitch.py)."""
    first, last = i, i
    while is_build(board, first):
        first -= 1
    while is_build(board, last + 1):
        last += 1
    return first, last


def build_base(outdir: Path, board: dict, i: int) -> Path | None:
    """The image a build frame adds to (frame i-1), or None for a normal frame."""
    return find_frame(outdir, i - 1) if is_build(board, i) else None


def later_adds(board: dict, i: int) -> list[str]:
    """What the rest of frame i's build group will add, in order."""
    return [board["frames"][k - 1]["adds"] for k in range(i + 1, group_of(board, i)[1] + 1)]


MAX_CAST_PER_FRAME = 3


def verify_board(board: dict) -> list[str]:
    """Pre-render checks on the storyboard, before any image is paid for. Fixes what it safely can
    (drops unknown cast names) and returns a warning per problem."""
    issues = []
    cast = board.get("cast")
    names = {c["name"] for c in cast} if isinstance(cast, list) else None
    if isinstance(cast, list) and not cast:
        issues.append("no cast")
    if not board.get("settings"):
        issues.append("no settings, so frames get no setting reference")
    for i, f in enumerate(board["frames"], 1):
        if names is not None and "cast" in f:
            unknown = [c for c in f["cast"] if c not in names]
            if unknown:
                issues.append(f"frame {i}: unknown cast {unknown} dropped")
                f["cast"] = [c for c in f["cast"] if c in names]
            if len(f["cast"]) > MAX_CAST_PER_FRAME:
                issues.append(f"frame {i}: {len(f['cast'])} characters (max {MAX_CAST_PER_FRAME})")
        if len(f["scene"].split()) < 8:
            issues.append(f"frame {i}: very short scene ({f['scene']!r})")
        if not f.get("camera") and not f.get("build"):
            issues.append(f"frame {i}: no valid camera, stitch uses its default")
        first, last = group_of(board, i)
        if i == last and last - first > MAX_BUILD_STEPS:
            issues.append(f"frames {first}-{last}: {last - first} build steps in a row (max {MAX_BUILD_STEPS})")
        cam = board["frames"][first - 1].get("camera") or {}
        if i == first + 1 and cam.get("move") in ("zoom_in", "pan"):
            issues.append(f"frame {first}: {cam['move']} on a build group can crop what's added later;"
                          " hold or zoom_out is safer")
    return issues


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


def setting_stem(name: str) -> str:
    return "setting-" + (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "place")


def find_setting(outdir: Path, name: str) -> Path | None:
    for ext in IMAGE_EXTS:
        p = outdir / f"{setting_stem(name)}{ext}"
        if p.exists():
            return p
    return None


def draw_settings(outdir: Path, board: dict, dry_run: bool = False) -> None:
    """Draw each setting once, empty, before any frame (setting-<name>.png). Every frame in that
    place is drawn against it: a clean anchor without characters, so frames don't copy a pose."""
    for st in board.get("settings", []):
        if find_setting(outdir, st["name"]):
            continue
        prompt = load_prompt("setting", place=st["name"], look=st["look"], background=st["background"])
        (outdir / f"{setting_stem(st['name'])}.txt").write_text(prompt)
        if dry_run:
            print(f"\n===== {setting_stem(st['name'])} =====\n{prompt}")
            continue
        print(f"{setting_stem(st['name'])}: drawing the empty setting...", flush=True)
        generate_image(prompt, references(), outdir, setting_stem(st["name"]))


def frame_images(outdir: Path, board: dict, i: int) -> Images:
    """What frame i is drawn against, in order: the series references, its setting reference,
    then the previous frame (poses/props only)."""
    images = references()
    st = setting_of(board, i)
    if st and (p := find_setting(outdir, st["name"])):
        images.append((f"Setting reference for {st['name']!r}: the empty place. Keep its layout and"
                       " background details exactly.", p))
    if is_build(board, i):  # the frame this one adds to
        if p := find_frame(outdir, i - 1):
            images.append((f"Previous frame (frame {i - 1}): this frame adds to it; everything in it"
                           " stays exactly as it is.", p))
        return images
    for k in range(max(1, i - PREV_FRAMES), i):
        if p := find_frame(outdir, k):
            images.append((f"Previous frame (frame {k}): for continuity of poses, props and positions"
                           " only; don't copy its layout.", p))
    return images


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
    text = ask_text(prompt)  # no reference images: in words only, so it doesn't copy their content
    try:
        board = json.loads(text[text.index("{") : text.rindex("}") + 1])
        frames = clean_frames(board.get("frames"), words)
        assert frames
        settings = clean_settings(board.get("settings"))
        fix_frame_settings(frames, settings)
    except (ValueError, KeyError, TypeError, AttributeError, AssertionError):
        print("warning: storyboard unusable, falling back to one frame per line", file=sys.stderr)
        board = {"cast": "The main character from the series bible."}
        settings = []
        frames = [{"start": s, "scene": line} for s, line in zip(line_starts(lines), lines)]
    board = with_text({"cast": clean_cast(board.get("cast", "")), "settings": settings, "frames": frames}, lines)
    for issue in verify_board(board):
        print(f"storyboard check: {issue}", file=sys.stderr)
    return board


def marked_script(board: dict, lines: list[str], i: int) -> str:
    s, e = spans(board, lines)[i - 1]
    w = script_words(lines)
    return " ".join([*w[:s], ">>>", *w[s:e], "<<<", *w[e:]])


def camera_note(board: dict, i: int) -> str:
    """Tells the drawing model where the camera move goes, so it puts the key detail there."""
    cam = board["frames"][i - 1].get("camera")
    if not cam or cam["move"] == "hold":
        return ""
    at = f"{cam['focus']['x']:.0%} across, {cam['focus']['y']:.0%} down"
    how = {"zoom_in": f"slowly zoom in towards the point {at}; put this frame's key detail there.",
           "zoom_out": f"start close on the point {at} and pull back to the whole frame; put the key"
                       " detail there and let the full frame reveal the rest.",
           "pan": f"slowly pan towards the point {at}; put this frame's key detail there."}[cam["move"]]
    return f"CAMERA: the video will {how}\n"


def frame_prompt(i: int, lines: list[str], board: dict) -> str:
    """Prompt for drawing frame i (sent after frame_images())."""
    n = len(board["frames"])
    f = board["frames"][i - 1]
    neighbour = lambda k, what: (f'{what}: "{frame_text(board, lines, k)}" - {board["frames"][k - 1]["scene"]}\n'
                                 if 1 <= k <= n else "")
    later = "; ".join(later_adds(board, i))
    if f.get("build"):
        todo = (f"STILL TO COME (later frames add these one at a time; don't draw them, keep their space"
                f" clear): {later}\n") if later else ""
        return load_prompt(
            "build", frame=i, prev=i - 1, n_frames=n, cast=cast_text(board), adds=f["adds"], later=todo,
            script=marked_script(board, lines, i), words=frame_text(board, lines, i), scene=f["scene"])
    buildup = (f"BUILD-UP: the next frames add to this picture one element at a time, like bullet points"
               f" appearing on a slide: {later}. Don't draw any of them yet, but leave clear, empty space"
               f" for each where it will go.\n") if later else ""
    continuity = f"CHANGED SINCE THE PREVIOUS FRAME: {f['continuity']}\n" if f.get("continuity") else ""
    return load_prompt(
        "frame", frame=i, n_frames=n, cast=cast_text(board), setting=setting_block(board, i),
        script=marked_script(board, lines, i), previous=neighbour(i - 1, "PREVIOUS FRAME"),
        words=frame_text(board, lines, i), scene=f["scene"], continuity=continuity,
        camera=camera_note(board, i), buildup=buildup,
        next=neighbour(i + 1, "NEXT FRAME (drawn after this one)"))


def prompt_record(prompt: str, images: Images) -> str:
    """What frame-NN.txt saves: the images sent (in order) and the prompt."""
    return "".join(f"[Image {k}] {label}  ({p.name})\n" for k, (label, p) in enumerate(images, 1)) + "\n" + prompt


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


def generate_image(prompt: str, images: Images, outdir: Path, stem: str) -> Path:
    """One image-model call: the labelled images (references first), then the prompt; a fresh,
    stateless call every time. Saves outdir/stem.<ext>. The aspect ratio is always set explicitly:
    otherwise the output takes the shape of the last input image."""
    r = get_client().interactions.create(
        model=IMAGE_MODEL,
        input=[*labelled(images), {"type": "text", "text": prompt}],
        response_format={"type": "image", "aspect_ratio": "9:16", "image_size": "1K"},
    )
    img = r.output_image
    if img is None:
        sys.exit(f"{stem}: no image returned. Model said: {r.output_text!r}")
    ext = ".jpg" if "jpeg" in (getattr(img, "mime_type", "") or "") else ".png"
    path = outdir / f"{stem}{ext}"
    path.write_bytes(base64.b64decode(img.data))
    return path


def generate_frame(prompt: str, images: Images, outdir: Path, stem: str, base: Path | None = None,
                   raw_dir: Path | None = None) -> Path:
    """generate_image(); for a build frame (base = the frame it adds to), then lock everything outside
    the addition to base (buildup.py), saved as outdir/stem.png. The unlocked drawing is kept in
    raw_dir (default outdir) as stem.raw.*."""
    if not base:
        return generate_image(prompt, images, outdir, stem)
    from buildup import lock

    raw_dir = raw_dir or outdir
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw = generate_image(prompt, images, raw_dir, f"{stem}.raw")
    return lock(base, raw, outdir / f"{stem}.png")


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
    print(f"\nCAST: {cast_text(board)}\n")
    for st in board.get("settings", []):
        print(f"SETTING {st['name']}: {st['look']}\n    background: {st['background']}\n")
    for i, ((s, e), f) in enumerate(zip(spans(board, lines), board["frames"]), 1):
        where = f"  [{f['setting']}]" if f.get("setting") else ""
        cam = f.get("camera")
        cam = (f"  {cam['move']} @ ({cam['focus']['x']:g}, {cam['focus']['y']:g})"
               + (f" x{cam['intensity']:g}" if "intensity" in cam else "")) if cam else ""
        who = f"  ({', '.join(f['cast'])})" if f.get("cast") else ""
        if f.get("build"):
            print(f"{i:02d}. ~{(e - s) / WORDS_PER_SECOND:.1f}s  + adds to {i - 1:02d}{who}  \"{f['text']}\"\n"
                  f"    + {f['adds']}\n")
            continue
        print(f"{i:02d}. ~{(e - s) / WORDS_PER_SECOND:.1f}s{where}{cam}{who}  \"{f['text']}\"\n    -> {f['scene']}\n"
              + (f"    changed: {f['continuity']}\n" if f.get("continuity") else ""))
    n = len(board["frames"]) + len(board.get("settings", []))
    builds = sum(1 for f in board["frames"] if f.get("build"))
    print(f"{len(board['frames'])} frames ({builds} build) + {len(board.get('settings', []))} setting images,"
          f" ~${n * IMAGE_COST:.2f} before any redraws. saved: {path}")
    for issue in verify_board(board):
        print(f"storyboard check: {issue}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("note", help="note ID, e.g. 001")
    ap.add_argument("--dry-run", action="store_true", help="write and print prompts, no API calls")
    ap.add_argument("--storyboard-only", action="store_true",
                    help="generate (or reuse) the storyboard, print it, and stop before images")
    ap.add_argument("--replan", "--force", action="store_true", dest="replan",
                    help="regenerate storyboard.json even if it exists, and so redraw every frame (old ones go"
                         " to history/before-replan-N/)")
    ap.add_argument("--no-qa", action="store_true", help="skip the per-frame quality check and redraws")
    ap.add_argument("--qa-only", action="store_true",
                    help="check the existing frames and report; draws nothing (for calibrating)")
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
        stale = sorted(p for p in [*outdir.glob("frame-*"), *outdir.glob("setting-*"), *outdir.glob("qa.json")]
                       if p.is_file())
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

    import qa  # here, so storyboard previews don't need Pillow

    if args.qa_only:
        qa.report(outdir, board, lines)
        return

    n = len(board["frames"])
    todo = sum(1 for i in range(1, n + 1) if not find_frame(outdir, i))
    todo += sum(1 for st in board.get("settings", []) if not find_setting(outdir, st["name"]))
    print(f"{n} frames, {todo} images to generate (~${todo * IMAGE_COST:.2f} before any redraws)")

    draw_settings(outdir, board, args.dry_run)
    for i in range(1, n + 1):
        stem = f"frame-{i:02d}"
        if find_frame(outdir, i):
            print(f"{stem}: exists, skipping")
            continue
        images, prompt = frame_images(outdir, board, i), frame_prompt(i, lines, board)
        (outdir / f"{stem}.txt").write_text(prompt_record(prompt, images))
        if args.dry_run:
            print(f"\n===== {stem} =====\n{prompt_record(prompt, images)}")
            continue

        t0 = time.monotonic()
        print(f"{time.strftime('%H:%M:%S')} {stem}: {frame_text(board, lines, i)[:60]}", flush=True)
        if args.no_qa:
            generate_frame(prompt, images, outdir, stem, build_base(outdir, board, i), outdir / "history")
        else:
            qa.draw_checked(outdir, board, lines, i, prompt, images)
        print(f"           done in {time.monotonic() - t0:.0f}s", flush=True)

    if not args.dry_run and not args.no_qa:
        qa.summary(outdir, board)
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
