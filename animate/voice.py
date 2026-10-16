#!/usr/bin/env python3
"""Generate the voiceover for a note's whole Description in one Gemini TTS call.

Usage:
  python3 animate/voice.py 001

The script is voiced in one go (not line by line) so the delivery flows: no hard
stop or change of energy between lines. align.py then finds when each word is
spoken, and stitch.py cuts frames and captions from those timings.

Output: animate/out/<id>/voice.wav, plus voice.json recording the exact text (and
voice/style/model) it was made from. It's reused only if that still matches; any
edit to the note, VOICE below or prompts/voice_style.txt re-records the whole clip.
"""

import argparse
import base64
import json
import sys

from animate import OUT, get_client, load_prompt, read_note, run_cli, script_text

TTS_MODEL = "gemini-3.8-flash-lite-tts"  # cheapest TTS; a 45s video costs ~1p
VOICE = "Sulafat"  # "warm"; full list in Google's speech-generation docs
STYLE = load_prompt("voice_style")  # delivery style; editing it re-records


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("note", help="note ID, e.g. 001")
    args = ap.parse_args()

    title, lines = read_note(args.note)
    text = script_text(lines)
    outdir = OUT / args.note
    outdir.mkdir(parents=True, exist_ok=True)
    print(f"{args.note}: {title!r}, {len(lines)} lines, {len(text.split())} words, voice {VOICE}")

    path, manifest_path = outdir / "voice.wav", outdir / "voice.json"
    made_from = {"text": text, "voice": VOICE, "style": STYLE, "model": TTS_MODEL}
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    if path.exists() and manifest == made_from:
        print(f"{path.name}: up to date, skipping")
        return
    print(f"{path.name} ({'changed' if path.exists() else 'new'}): recording the whole script...")
    r = get_client().interactions.create(
        model=TTS_MODEL,
        input=[{
            "type": "user_input",
            "content": [{
                "type": "text",
                "text": text,
                "annotations": [{"type": "speech_metadata", "style": STYLE}],
            }],
        }],
        response_format={"type": "audio"},  # WAV, 24 kHz mono
        generation_config={"speech_config": [{"voice": VOICE}]},
    )
    if r.output_audio is None:
        sys.exit(f"{path.name}: no audio returned. Model said: {r.output_text!r}")
    path.write_bytes(base64.b64decode(r.output_audio.data))
    manifest_path.write_text(json.dumps(made_from, indent=2))
    print(f"done: {path}")


if __name__ == "__main__":
    run_cli(main)
