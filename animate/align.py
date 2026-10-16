#!/usr/bin/env python3
"""Find when each word of the script is spoken in the voiceover (forced alignment).

Usage:
  python3 animate/align.py 001

We already know the exact words (the note) and have the audio (voice.wav), so
instead of transcribing (which can mishear) we *align*: Whisper, via stable-ts,
works out when each known word is spoken.

Output: animate/out/<id>/words.json, one [word, start, end] per word of the
script (whitespace-separated, as in the note), in seconds from the start of
voice.wav. Redone whenever voice.wav is newer. stitch.py cuts frames and times
captions from it; without it, it estimates.

Needs: stable-ts (in requirements.txt, installed by `just setup`; pulls in
PyTorch) and a ~150MB model, downloaded once on first run.
"""

import argparse
import json
import sys

from animate import OUT, read_note, run_cli, script_text, script_timings, script_words

WHISPER_MODEL = "base.en"  # small and fast; alignment doesn't need a big model


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("note", help="note ID, e.g. 001")
    args = ap.parse_args()

    _, lines = read_note(args.note)
    outdir = OUT / args.note
    wav, out = outdir / "voice.wav", outdir / "words.json"
    if not wav.exists():
        print(f"{wav.name}: missing, skipping (run `just voice {args.note}`)")
        return
    if out.exists() and out.stat().st_mtime >= wav.stat().st_mtime:
        print(f"{out.name}: up to date, skipping")
        return

    try:
        import stable_whisper
    except ImportError:
        sys.exit("stable-ts not installed: run `just setup`")
    print(f"loading whisper model {WHISPER_MODEL}...")
    model = stable_whisper.load_model(WHISPER_MODEL)

    result = model.align(str(wav), script_text(lines), language="en")
    timed = [[w.word, w.start, w.end] for w in result.all_words() if w.word.strip()]
    words = script_timings(script_words(lines), timed)
    out.write_text(json.dumps(words))
    print(f"{out.name}: {len(words)} words, {words[-1][2]:.1f}s")


if __name__ == "__main__":
    run_cli(main)
