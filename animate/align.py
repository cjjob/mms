#!/usr/bin/env python3
"""Find when each word is spoken in each voice clip (forced alignment).

Usage:
  python3 animate/align.py 001

We already know the exact words (the note) and have the audio (voice-NN.wav),
so instead of transcribing (which can mishear) we *align*: Whisper, via
stable-ts, works out when each known word is spoken.

Output: animate/out/<id>/words-NN.json, a list of [word, start, end] in
seconds from the start of that clip. stitch.py uses these for word-by-word
captions; without them it estimates timings. Existing files are skipped.

Needs: stable-ts (in requirements.txt, installed by `just setup`; pulls in
PyTorch) and a ~150MB model, downloaded once on first run.
"""

import argparse
import json
import sys

from animate import OUT, read_note, run_cli

WHISPER_MODEL = "base.en"  # small and fast; alignment doesn't need a big model


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("note", help="note ID, e.g. 001")
    args = ap.parse_args()

    _, lines = read_note(args.note)
    outdir = OUT / args.note
    todo = []
    for i, line in enumerate(lines, 1):
        wav, out = outdir / f"voice-{i:02d}.wav", outdir / f"words-{i:02d}.json"
        if out.exists():
            print(f"{out.name}: exists, skipping")
        elif not wav.exists():
            print(f"{wav.name}: missing, skipping (run `just voice {args.note}`)")
        else:
            todo.append((line, wav, out))
    if not todo:
        return

    try:
        import stable_whisper
    except ImportError:
        sys.exit("stable-ts not installed: run `just setup`")
    print(f"loading whisper model {WHISPER_MODEL}...")
    model = stable_whisper.load_model(WHISPER_MODEL)

    for line, wav, out in todo:
        result = model.align(str(wav), line, language="en")
        words = [[w.word.strip(), round(w.start, 3), round(w.end, 3)]
                 for w in result.all_words() if w.word.strip()]
        out.write_text(json.dumps(words))
        print(f"{out.name}: {len(words)} words")

    print(f"done: {outdir}")


if __name__ == "__main__":
    run_cli(main)
