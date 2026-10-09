#!/usr/bin/env python3
"""Stitch a note's frames (and voiceover, if generated) into a vertical MP4.

Usage:
  python3 animate/stitch.py 001

Each frame-NN is shown for the length of voice-NN.wav plus a short pause, or
for SILENT_SECONDS if there's no clip, with a slow zoom-in. Output is
animate/out/<id>.mp4 at 1080x1920, 30fps, H.264/AAC (TikTok/Reels-ready),
plus <id>.ass, the caption file that was burned in.

Captions: TikTok style, a few words at a time with the spoken word
highlighted and popping. Word timings come from words-NN.json (`just align`)
when present, otherwise they're estimated from each clip's length.

Needs ffmpeg built with libass for captions. Homebrew's default `ffmpeg` is a
lite build without it; use `ffmpeg-full` (see has_libass below).
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

from animate import OUT, find_frame, load_board, read_note, run_cli

HERE = Path(__file__).resolve().parent

# Video
W, H, FPS = 1080, 1920, 30
PAUSE = 0.4           # seconds of silence after each line
SILENT_SECONDS = 3.0  # frame length when there's no voice clip
ZOOM = 0.05           # how far each frame zooms in over its duration (5%); 0 = still

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


def settings(board: dict | None, i: int) -> tuple[float, float]:
    """(zoom, pause) for frame i: per-frame overrides from storyboard.json, else defaults."""
    ov = ((board or {}).get("overrides") or {}).get(str(i), {})
    return float(ov.get("zoom", ZOOM)), float(ov.get("pause", PAUSE))


def wav_seconds(path: Path) -> float:
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


def segment(img: Path, voice: Path | None, seconds: float, out: Path, zoom: float = ZOOM) -> float:
    """Render one frame as a clip; returns its exact duration."""
    n = round(seconds * FPS)
    # Upscale 2x before zoompan to avoid jitter, fill 9:16 by cropping, then zoom towards the centre.
    vf = (
        f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,crop={W * 2}:{H * 2},"
        f"zoompan=z='{1 + max(0.0, -zoom)}+{zoom}*on/{n}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":d=1:s={W}x{H}:fps={FPS},format=yuv420p"
    )
    audio = ["-i", str(voice)] if voice else ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    subprocess.run([
        "ffmpeg", "-loglevel", "error", "-y",
        "-loop", "1", "-framerate", str(FPS), "-i", str(img), *audio,
        "-filter_complex", f"[0:v]{vf}[v];[1:a]aresample=48000,aformat=channel_layouts=stereo,apad[a]",
        "-map", "[v]", "-map", "[a]", "-frames:v", str(n), "-t", f"{n / FPS:.3f}",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-r", str(FPS),
        "-c:a", "aac", "-b:a", "160k",
        str(out),
    ], check=True)
    return n / FPS


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
    args = argparse.Namespace(note=note)
    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found: brew install ffmpeg-full")
    if not has_libass():
        sys.exit(LIBASS_FIX)
    _, lines = read_note(args.note)
    outdir = OUT / args.note
    board = load_board(outdir)
    imgs = frames(outdir)
    if not imgs:
        sys.exit(f"no frames in {outdir}; run `just animate {args.note}` first")
    if len(imgs) != len(lines):
        print(f"warning: {len(imgs)} frames but {len(lines)} lines in the note", file=sys.stderr)

    voiced = sum((outdir / f"voice-{i:02d}.wav").exists() for i in range(1, len(imgs) + 1))
    if not voiced:
        print(f"NOTE: no voiceover found, so this video will be SILENT. Run `just voice {args.note}` first.")
    elif voiced < len(imgs):
        print(f"NOTE: only {voiced}/{len(imgs)} lines have a voiceover; the rest will be silent.")

    offset, events, estimated = 0.0, [], 0
    final = OUT / f"{args.note}.mp4"
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        parts = []
        for i, img in enumerate(imgs, 1):
            zoom, pause = settings(board, i)
            voice = outdir / f"voice-{i:02d}.wav"
            if voice.exists():
                speech = wav_seconds(voice)
                seconds = speech + pause
            else:
                print(f"{img.stem}: no {voice.name}, using {SILENT_SECONDS}s of silence")
                voice, speech, seconds = None, SILENT_SECONDS - PAUSE, SILENT_SECONDS
            part = tmp / f"{img.stem}.mp4"
            extra = "" if (zoom, pause) == (ZOOM, PAUSE) else f"  (zoom {zoom:g}, pause {pause:g}s)"
            print(f"{img.stem}: {seconds:.1f}s{extra}")
            seconds = segment(img, voice, seconds, part, zoom)
            parts.append(part)

            if i <= len(lines):
                words_file = outdir / f"words-{i:02d}.json"
                if words_file.exists():
                    words = json.loads(words_file.read_text())
                else:
                    words = estimate_words(lines[i - 1], speech)
                    estimated += 1
                events += ass_events(words, offset, seconds)
            offset += seconds

        listing = tmp / "list.txt"
        listing.write_text("".join(f"file '{p}'\n" for p in parts))
        joined = tmp / "joined.mp4"
        subprocess.run([
            "ffmpeg", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
            "-c", "copy", str(joined),
        ], check=True)

        if estimated:
            print(f"captions: estimated timings for {estimated} line(s); run `just align {args.note}` for exact ones")
        ass = OUT / f"{args.note}.ass"
        ass.write_text(ass_file(events))
        shutil.copy(ass, tmp / "captions.ass")
        vf = "ass=captions.ass"
        if (HERE / "fonts").is_dir():
            vf += f":fontsdir='{HERE / 'fonts'}'"
        print("burning in captions...")
        subprocess.run([
            "ffmpeg", "-loglevel", "error", "-y", "-i", "joined.mp4", "-vf", vf,
            "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "copy",
            "-movflags", "+faststart", str(final),
        ], check=True, cwd=tmp)

    print(f"done: {final} ({offset:.0f}s)")


if __name__ == "__main__":
    run_cli(main)
