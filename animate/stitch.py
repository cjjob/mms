#!/usr/bin/env python3
"""Stitch a note's frames (and voiceover, if generated) into a vertical MP4.

Usage:
  python3 animate/stitch.py 001

The narration is one continuous track: each line's voice-NN.wav plus a short
pause (or SILENT_SECONDS of silence if there's no clip). Frames follow the
storyboard: each one appears as its first word is spoken (word timings from
words-NN.json, or estimated) and stays until the next, with a slow zoom-in. Output is
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
import re
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

from animate import OUT, find_frame, line_starts, load_board, read_note, run_cli

HERE = Path(__file__).resolve().parent

# Video
W, H, FPS = 1080, 1920, 30
PAUSE = 0.4           # seconds of silence after each line
SILENT_SECONDS = 3.0  # frame length when there's no voice clip
ZOOM = 0.05           # how far each frame zooms in over its duration (5%); 0 = still
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


def settings(board: dict | None, i: int) -> float:
    """Zoom for frame i: per-frame override from storyboard.json, else the default."""
    ov = ((board or {}).get("overrides") or {}).get(str(i), {})
    return float(ov.get("zoom", ZOOM))


def wav_seconds(path: Path) -> float:
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


def segment(img: Path, n: int, out: Path, zoom: float = ZOOM) -> None:
    """Render one frame as a silent clip of exactly n video frames."""
    # Upscale 2x before zoompan to avoid jitter, fill 9:16 by cropping, then zoom towards the centre.
    vf = (
        f"scale={W * 2}:{H * 2}:force_original_aspect_ratio=increase,crop={W * 2}:{H * 2},"
        f"zoompan=z='{1 + max(0.0, -zoom)}+{zoom}*on/{n}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":d=1:s={W}x{H}:fps={FPS},format=yuv420p"
    )
    subprocess.run([
        "ffmpeg", "-loglevel", "error", "-y", "-loop", "1", "-framerate", str(FPS), "-i", str(img),
        "-vf", vf, "-frames:v", str(n), "-an",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-r", str(FPS), str(out),
    ], check=True)


def narration(clips: list[tuple[Path | None, float]], out: Path) -> None:
    """One continuous audio track: each (clip or silence) padded to its duration, back to back."""
    inputs, chains = [], []
    for k, (clip, dur) in enumerate(clips):
        inputs += ["-i", str(clip)] if clip else ["-f", "lavfi", "-t", f"{dur:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
        chains.append(f"[{k}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                      f"apad=whole_dur={dur:.3f},atrim=0:{dur:.3f}[a{k}]")
    graph = ";".join(chains) + ";" + "".join(f"[a{k}]" for k in range(len(clips))) + \
        f"concat=n={len(clips)}:v=0:a=1[out]"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *inputs, "-filter_complex", graph,
                    "-map", "[out]", "-c:a", "pcm_s16le", str(out)], check=True)


def word_starts(line: str, words: list[list]) -> list[float]:
    """Start time of each whitespace-separated word of `line`, from timed words that may be
    tokenised differently (stable-ts splits some words): matched by their letters/digits."""
    norm = lambda t: re.sub(r"\W", "", t.lower())
    times = []  # one entry per letter/digit across the timed words
    for w, start, end in words:
        nw = norm(w)
        times += [start + (end - start) * k / max(1, len(nw)) for k in range(len(nw))]
    mine = line.split()
    total = sum(len(norm(w)) for w in mine) or 1
    out, pos = [], 0
    for w in mine:
        k = min(len(times) - 1, round(pos * len(times) / total)) if times else 0
        out.append(times[k] if times else 0.0)
        pos += len(norm(w))
    return out


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

    manifest_path = outdir / "voice.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    stale = [i for i, line in enumerate(lines, 1) if (outdir / f"voice-{i:02d}.wav").exists()
             and (manifest.get(f"{i:02d}") or {}).get("text") != line]
    if stale:
        sys.exit(f"voice clips don't match the note for line(s) {', '.join(map(str, stale))}; "
                 f"run `just voice {note}` and `just align {note}` (or `just video {note}`)")
    voiced = sum((outdir / f"voice-{i:02d}.wav").exists() for i in range(1, len(lines) + 1))
    if not voiced:
        print(f"NOTE: no voiceover found, so this video will be SILENT. Run `just voice {note}` first.")
    elif voiced < len(lines):
        print(f"NOTE: only {voiced}/{len(lines)} lines have a voiceover; the rest will be silent.")

    # Timeline: lines back to back; every word gets an absolute start time.
    offset, clips, events, word_t, estimated = 0.0, [], [], [], 0
    first_words = set(line_starts(lines))
    for i, line in enumerate(lines, 1):
        voice = outdir / f"voice-{i:02d}.wav"
        if voice.exists():
            speech = wav_seconds(voice)
            dur = speech + PAUSE
        else:
            voice, speech, dur = None, SILENT_SECONDS - PAUSE, SILENT_SECONDS
        words_file = outdir / f"words-{i:02d}.json"
        if words_file.exists():
            words = json.loads(words_file.read_text())
        else:
            words = estimate_words(line, speech)
            estimated += 1
        word_t += [offset + t for t in word_starts(line, words)]
        events += ass_events(words, offset, dur)
        clips.append((voice, dur))
        line_offset = offset
        offset += dur
        word_t[len(word_t) - len(line.split())] = line_offset  # line's first word: cut at line start
    total = offset

    # Frame boundaries in video frames: each appears just before its first word.
    t = [0.0] + [word_t[s] if s in first_words else max(0.0, word_t[s] - LEAD) for s in starts[1:]]
    bounds = [round(x * FPS) for x in t] + [round(total * FPS)]
    for k in range(1, len(bounds)):  # every frame at least 1 video frame long
        bounds[k] = max(bounds[k], bounds[k - 1] + 1)

    final = OUT / f"{note}.mp4"
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        parts = []
        for i, img in enumerate(imgs, 1):
            n, zoom = bounds[i] - bounds[i - 1], settings(board, i)
            extra = "" if zoom == ZOOM else f"  (zoom {zoom:g})"
            print(f"{img.stem}: {n / FPS:.1f}s{extra}")
            part = tmp / f"{img.stem}.mp4"
            segment(img, n, part, zoom)
            parts.append(part)

        (tmp / "list.txt").write_text("".join(f"file '{p}'\n" for p in parts))
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                        "-i", str(tmp / "list.txt"), "-c", "copy", str(tmp / "joined.mp4")], check=True)
        narration(clips, tmp / "narration.wav")

        if estimated:
            print(f"timings: estimated for {estimated} line(s); run `just align {note}` for exact ones")
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
