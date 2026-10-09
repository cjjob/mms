#!/usr/bin/env python3
"""Generate one voiceover clip per Description line with Gemini TTS.

Usage:
  python3 animate/voice.py 001

Output: animate/out/<id>/voice-NN.wav, matching frame-NN. Existing clips are
skipped; delete one to regenerate it. Change VOICE/STYLE below to taste.
"""

import argparse
import base64
import sys

from animate import OUT, get_client, read_note, run_cli

TTS_MODEL = "gemini-3.8-flash-lite-tts"  # cheapest TTS; a 45s video costs ~1p
VOICE = "Sulafat"  # "warm"; full list in Google's speech-generation docs
STYLE = "calm, warm and sincere; unhurried, like an encouraging friend"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("note", help="note ID, e.g. 001")
    args = ap.parse_args()

    title, lines = read_note(args.note)
    outdir = OUT / args.note
    outdir.mkdir(parents=True, exist_ok=True)
    print(f"{args.note}: {title!r}, {len(lines)} lines, voice {VOICE}")

    for i, line in enumerate(lines, 1):
        path = outdir / f"voice-{i:02d}.wav"
        if path.exists():
            print(f"{path.name}: exists, skipping")
            continue
        print(f"{path.name}: {line[:60]}")
        r = get_client().interactions.create(
            model=TTS_MODEL,
            input=[{
                "type": "user_input",
                "content": [{
                    "type": "text",
                    "text": line,
                    "annotations": [{"type": "speech_metadata", "style": STYLE}],
                }],
            }],
            response_format={"type": "audio"},  # WAV, 24 kHz mono
            generation_config={"speech_config": [{"voice": VOICE}]},
        )
        if r.output_audio is None:
            sys.exit(f"{path.name}: no audio returned. Model said: {r.output_text!r}")
        path.write_bytes(base64.b64decode(r.output_audio.data))

    print(f"done: {outdir}")


if __name__ == "__main__":
    run_cli(main)
