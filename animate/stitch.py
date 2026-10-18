#!/usr/bin/env python3
"""Stitch a note's frames (and voiceover, if generated) into a vertical MP4.

Usage:
  python3 animate/stitch.py 001

The narration is voice.wav, the whole script voiced in one go (or, before
`just voice`, SILENT_SECONDS of silence per line). Frames follow the
storyboard: each one appears as its first word is spoken (word timings from
words.json, or estimated) and stays until the next, with its storyboard camera move
(hold / zoom in / zoom out / pan, a crop-and-scale over the frame's duration). Output is
animate/out/<id>.mp4 at 1080x1920, 30fps, H.264/AAC (TikTok/Reels-ready),
plus <id>.ass, the caption file that was burned in.

Captions: TikTok style, a few words at a time with the spoken word
highlighted and popping, never spanning two lines of the note. Word timings
come from words.json (`just align`) when present, otherwise they're estimated
from the clip's length.

Needs ffmpeg built with libass for captions. Homebrew's default `ffmpeg` is a
lite build without it; use `ffmpeg-full` (see has_libass below).
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

from animate import OUT, find_frame, line_starts, load_board, read_note, run_cli, script_text, script_words

HERE = Path(__file__).resolve().parent

# Video
W, H, FPS = 1080, 1920, 30
TAIL = 0.4            # seconds held after the voiceover ends
SILENT_SECONDS = 3.0  # seconds per line when there's no voiceover yet
ZOOM = 0.05           # zoom-in for frames with no camera in the storyboard (5%); 0 = still
MAX_ZOOM = 0.25       # zoom (or, for a pan, the crop that leaves room to move) at camera intensity 1
INTENSITY = 0.5       # camera intensity when the storyboard doesn't give one
LEAD = 0.08           # a frame appears this many seconds before its first word

# Captions (ASS colours are &HAABBGGRR)
FONT = "Arial Black"          # ships with macOS; or drop a .ttf in animate/fonts/ and name it here
FONT_SIZE = 80
UPPERCASE = False
TEXT_COLOUR = "&H00FFFFFF"    # white
OUTLINE_COLOUR = "&H00000000"  # black
HIGHLIGHT = "&H0000D4FF"      # #FFD400 yellow, for the word being spoken
OUTLINE, SHADOW = 7, 3
MARGIN_V = 430                # px up from the bottom: clear of TikTok's caption/buttons
MAX_WORDS, MAX_CHARS = 5, 24  # per on-screen chunk
POP = 115                     # spoken word scales to this % and back


# ---------- video ----------

def frames(outdir: Path) -> list[Path]:
    out, i = [], 1
    while (p := find_frame(outdir, i)):
        out.append(p)
        i += 1
    return out


def camera(board: dict | None, i: int) -> tuple[str, float, float, float]:
    """(move, focus x, focus y, amount) for frame i. The storyboard's camera, unless `just edit`
    set a zoom override (sign picks in/out; it keeps the focus). No camera: centred ZOOM zoom-in."""
    frames = (board or {}).get("frames") or []
    cam = (frames[i - 1].get("camera") if i <= len(frames) else None) or {}
    focus = cam.get("focus") or {}
    x, y = float(focus.get("x", 0.5)), float(focus.get("y", 0.5))
    if cam:
        move, amount = cam["move"], MAX_ZOOM * float(cam.get("intensity", INTENSITY))
    else:
        move, amount = "zoom_in", ZOOM
    ov = ((board or {}).get("overrides") or {}).get(str(i), {})
    if "zoom" in ov:
        z = float(ov["zoom"])
        move, amount = ("hold" if z == 0 else "zoom_in" if z > 0 else "zoom_out"), abs(z)
    return move, x, y, amount


def settings(board: dict | None, i: int) -> float:
    """Frame i's zoom as one signed number (+ in, - out, 0 hold/pan), for `just edit`."""
    move, _, _, amount = camera(board, i)
    return {"zoom_in": amount, "zoom_out": -amount}.get(move, 0.0)


def describe_camera(move: str, x: float, y: float, amount: float) -> str:
    return "hold" if move == "hold" else f"{move} {amount:.0%} @ ({x:g}, {y:g})"


def wav_seconds(path: Path) -> float:
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


def segment(img: Path, n: int, out: Path, cam: tuple = ("zoom_in", 0.5, 0.5, ZOOM)) -> None:
    """Render one frame as a silent clip of exactly n video frames, with camera move cam
    (from camera()): a crop that scales and/or slides linearly over the clip.
      hold      the whole image, still
      zoom_in   crop shrinks from the whole image to 1/(1+amount) of it, centred on the focus
      zoom_out  the reverse
      pan       crop fixed at 1/(1+amount), sliding from the opposite side towards the focus
    The crop is clamped inside the image, so a focus near an edge zooms as close as it can."""
    move, x, y, amount = cam
    z0, z1 = {"zoom_in": (1, 1 + amount), "zoom_out": (1 + amount, 1), "pan": (1 + amount, 1 + amount)}.get(move, (1, 1))
    (x0, y0) = (1 - x, 1 - y) if move == "pan" else (x, y)  # crop centre at the start and end, as fractions
    t = f"on/{max(1, n - 1)}"
    lerp = lambda a, b: f"({a:.4f}+({b - a:.4f})*{t})"
    # Fill 9:16 by cropping (at 2x, to avoid zoompan jitter), then crop-and-scale per the move.
    vf = (
        f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,crop={W * 2}:{H * 2},"
        f"zoompan=z='{lerp(z0, z1)}'"
        f":x='max(0,min(iw-iw/zoom,{lerp(x0, x)}*iw-iw/zoom/2))'"
        f":y='max(0,min(ih-ih/zoom,{lerp(y0, y)}*ih-ih/zoom/2))'"
        f":d=1:s={W}x{H}:fps={FPS},format=yuv420p"
    )
    subprocess.run([
        "ffmpeg", "-loglevel", "error", "-y", "-loop", "1", "-framerate", str(FPS), "-i", str(img),
        "-vf", vf, "-frames:v", str(n), "-an",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-r", str(FPS), str(out),
    ], check=True)


def narration(clip: Path | None, dur: float, out: Path) -> None:
    """The audio track: the voiceover (or silence) padded/trimmed to exactly dur seconds."""
    src = ["-i", str(clip)] if clip else ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *src, "-af",
                    f"aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                    f"apad=whole_dur={dur:.3f},atrim=0:{dur:.3f}",
                    "-c:a", "pcm_s16le", str(out)], check=True)


# ---------- captions ----------

def estimate_words(line: str, seconds: float) -> list[list]:
    """No alignment: spread the clip's duration over words, weighted by length."""
    words = line.split()
    weights = [len(w) + 2 for w in words]
    total, t, out = sum(weights), 0.0, []
    for w, k in zip(words, weights):
        d = seconds * k / total
        out.append([w, t, t + d])
        t += d
    return out


def chunks(words: list[list]) -> list[list[list]]:
    """Group words into short on-screen chunks, breaking after punctuation."""
    out, cur = [], []
    for w in words:
        if cur and (len(cur) >= MAX_WORDS or len(" ".join(x[0] for x in cur + [w])) > MAX_CHARS):
            out.append(cur)
            cur = []
        cur.append(w)
        if w[0][-1] in ".,!?;:…-—":
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def ts(t: float) -> str:
    cs = max(0, round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def esc(text: str) -> str:
    text = text.upper() if UPPERCASE else text
    return text.replace("\\", "").replace("{", "(").replace("}", ")")


def ass_events(words: list[list], offset: float, line_end: float) -> list[str]:
    """One event per word: the whole chunk visible, the current word highlighted."""
    events = []
    groups = chunks(words)
    for gi, group in enumerate(groups):
        chunk_end = groups[gi + 1][0][1] if gi + 1 < len(groups) else min(group[-1][2] + 0.3, line_end)
        for wi, (_, start, _) in enumerate(group):
            end = group[wi + 1][1] if wi + 1 < len(group) else chunk_end
            if wi == 0:
                start = group[0][1]
            text = " ".join(
                (f"{{\\c{HIGHLIGHT}&\\t(0,90,\\fscx{POP}\\fscy{POP})\\t(90,180,\\fscx100\\fscy100)}}{esc(w)}{{\\r}}"
                 if j == wi else esc(w))
                for j, (w, _, _) in enumerate(group)
            )
            if end > start:
                events.append(f"Dialogue: 0,{ts(offset + start)},{ts(offset + end)},Default,,0,0,0,,{text}")
    return events


def ass_file(events: list[str]) -> str:
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{FONT},{FONT_SIZE},{TEXT_COLOUR},{TEXT_COLOUR},{OUTLINE_COLOUR},&H80000000,0,0,0,0,100,100,0,0,1,{OUTLINE},{SHADOW},2,90,90,{MARGIN_V},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""" + "\n".join(events) + "\n"


LIBASS_FIX = (
    "your ffmpeg has no libass, which captions need (Homebrew's default ffmpeg is a lite build).\n"
    "Fix: brew install ffmpeg-full && brew unlink ffmpeg && brew link --force --overwrite ffmpeg-full"
)


def has_libass() -> bool:
    out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    return any(line.split()[1:2] == ["ass"] for line in out.splitlines())


# ---------- main ----------

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("note", help="note ID, e.g. 001")
    stitch(ap.parse_args().note)


def stitch(note: str) -> None:
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found: brew install ffmpeg-full")
    if not has_libass():
        sys.exit(LIBASS_FIX)
    _, lines = read_note(note)
    outdir = OUT / note
    board = load_board(outdir, lines)
    starts = [f["start"] for f in board["frames"]] if board else line_starts(lines)
    imgs = frames(outdir)
    if not imgs:
        sys.exit(f"no frames in {outdir}; run `just animate {note}` first")
    if len(imgs) < len(starts):
        sys.exit(f"{len(imgs)} frames but the storyboard has {len(starts)}; run `just animate {note}`")
    imgs = imgs[:len(starts)]

    # Timeline: one [word, start, end] per script word, in seconds from the start of the video.
    words = script_words(lines)
    voice, manifest_path = outdir / "voice.wav", outdir / "voice.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    estimated = False
    if voice.exists():
        if manifest.get("text") != script_text(lines):
            sys.exit(f"voiceover doesn't match the note; run `just voice {note}` and `just align {note}` "
                     f"(or `just video {note}`)")
        speech = wav_seconds(voice)
        total = speech + TAIL
        words_file = outdir / "words.json"
        timed = json.loads(words_file.read_text()) if words_file.exists() else None
        if not timed or [w[0] for w in timed] != words:
            timed, estimated = estimate_words(script_text(lines), speech), True
    else:
        print(f"NOTE: no voiceover found, so this video will be SILENT. Run `just voice {note}` first.")
        voice, timed, total = None, [], 0.0
        for line in lines:
            timed += [[w, total + s, total + e] for w, s, e in estimate_words(line, SILENT_SECONDS - TAIL)]
            total += SILENT_SECONDS

    # Captions line by line, so a chunk never runs from one line into the next.
    events, bounds_w = [], line_starts(lines) + [len(words)]
    for s, e in zip(bounds_w, bounds_w[1:]):
        events += ass_events(timed[s:e], 0.0, timed[e][1] if e < len(words) else total)

    # Frame boundaries in video frames: each appears just before its first word.
    t = [0.0] + [max(0.0, timed[s][1] - LEAD) for s in starts[1:]]
    bounds = [round(x * FPS) for x in t] + [round(total * FPS)]
    for k in range(1, len(bounds)):  # every frame at least 1 video frame long
        bounds[k] = max(bounds[k], bounds[k - 1] + 1)

    final = OUT / f"{note}.mp4"
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        parts = []
        for i, img in enumerate(imgs, 1):
            n, cam = bounds[i] - bounds[i - 1], camera(board, i)
            print(f"{img.stem}: {n / FPS:.1f}s  {describe_camera(*cam)}")
            part = tmp / f"{img.stem}.mp4"
            segment(img, n, part, cam)
            parts.append(part)

        (tmp / "list.txt").write_text("".join(f"file '{p}'\n" for p in parts))
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                        "-i", str(tmp / "list.txt"), "-c", "copy", str(tmp / "joined.mp4")], check=True)
        narration(voice, total, tmp / "narration.wav")

        if estimated:
            print(f"timings: estimated; run `just align {note}` for exact ones")
        ass = OUT / f"{note}.ass"
        ass.write_text(ass_file(events))
        shutil.copy(ass, tmp / "captions.ass")
        vf = "ass=captions.ass"
        if (HERE / "fonts").is_dir():
            vf += f":fontsdir='{HERE / 'fonts'}'"
        print("burning in captions...")
        subprocess.run([
            "ffmpeg", "-loglevel", "error", "-y", "-i", "joined.mp4", "-i", "narration.wav",
            "-map", "0:v", "-map", "1:a", "-vf", vf,
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", str(final),
        ], check=True, cwd=tmp)

    print(f"done: {final} ({total:.0f}s, {len(imgs)} frames)")


if __name__ == "__main__":
    run_cli(main)
