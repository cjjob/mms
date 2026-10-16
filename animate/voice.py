#!/usr/bin/env python3
"""Generate one voiceover clip per Description line with Gemini TTS.

Usage:
  python3 animate/voice.py 001

Output: animate/out/<id>/voice-NN.wav, one per line of the note, plus voice.json
recording the exact text (and voice/style) each clip was made from. A clip is
reused only if that still matches; edit a line in the note and just that line is
re-recorded. Change VOICE below or prompts/voice_style.txt to taste (that re-records everything).
"""

import argparse
import base64
import json
import sys

from animate import OUT, get_client, load_prompt, read_note, run_cli

TTS_MODEL = "gemini-3.8-flash-lite-tts"  # cheapest TTS; a 45s video costs ~1p
VOICE = "Sulafat"  # "warm"; full list in Google's speech-generation docs
STYLE = load_prompt("voice_style")  # delivery style; editing it re-records every clip


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("note", help="note ID, e.g. 001")
    args = ap.parse_args()

    title, lines = read_note(args.note)
    outdir = OUT / args.note
    outdir.mkdir(parents=True, exist_ok=True)
    print(f"{args.note}: {title!r}, {len(lines)} lines, voice {VOICE}")

    manifest_path = outdir / "voice.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    for i, line in enumerate(lines, 1):
        path = outdir / f"voice-{i:02d}.wav"
        made_from = {"text": line, "voice": VOICE, "style": STYLE, "model": TTS_MODEL}
        if path.exists() and manifest.get(f"{i:02d}") == made_from:
            print(f"{path.name}: up to date, skipping")
            continue
        why = "changed" if path.exists() else "new"
        print(f"{path.name} ({why}): {line[:60]}")
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
        manifest[f"{i:02d}"] = made_from
        manifest_path.write_text(json.dumps(manifest, indent=2))  # after each clip, so a crash loses nothing

    print(f"done: {outdir}")


if __name__ == "__main__":
    run_cli(main)
