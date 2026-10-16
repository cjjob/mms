#!/usr/bin/env python3
"""Generate the voiceover for a note's whole Description in one TTS call.

Usage:
  python3 animate/voice.py 001
  python3 animate/voice.py 001 --retake   # re-record even if nothing changed (new random seed)

The script is voiced in one go (not line by line) so the delivery flows: no hard
stop or change of energy between lines. Word timings then come from align.py
(Gemini), or straight from ElevenLabs, and stitch.py cuts frames and captions
from them.

Backend: VOICE_BACKEND in .env (or the shell), `google` (default, Gemini TTS) or
`elevenlabs` (needs ELEVENLABS_API_KEY). ElevenLabs returns per-character
timings with the audio, so it writes words.json too and `just align` has
nothing to do.

Output: animate/out/<id>/voice.wav, plus voice.json recording the exact text (and
backend/voice/style/model) it was made from. It's reused only if that still
matches; any edit to the note or the settings below re-records the whole clip.

Seed (ElevenLabs): with ELEVEN_SEED = None every take uses a fresh random seed,
printed and saved in voice.json as "seed_used". Found a take you like? Copy that
number into ELEVEN_SEED to keep it (ElevenLabs aims for, but doesn't guarantee,
the same audio for the same seed, text and settings).
"""

import argparse
import base64
import json
import os
import random
import sys
import urllib.error
import urllib.request

from animate import (OUT, env_choice, get_client, load_env, load_prompt, read_note, run_cli, script_text,
                     script_timings, script_words)

# Gemini (VOICE_BACKEND=google)
TTS_MODEL = "gemini-3.8-flash-lite-tts"  # cheapest TTS; a 45s video costs ~1p
VOICE = "Sulafat"  # "warm"; full list in Google's speech-generation docs
STYLE = load_prompt("voice_style")  # delivery style; editing it re-records (Gemini only)

# ElevenLabs (VOICE_BACKEND=elevenlabs)
ELEVEN_VOICE = "EST9Ui6982FZPSi7gCHi"
ELEVEN_MODEL = "eleven_v4_exp"  # what the website used (`just eleven-history`); if the API rejects it, use "eleven_v4"
ELEVEN_SETTINGS = {"stability": 0.5, "similarity_boost": 0.7}  # from the website take; e.g. add "speed": 1.1
# (the website's "similarity" is the API's "similarity_boost"; its text_boost/focus aren't in the API)
ELEVEN_SEED = None  # e.g. 123456789 to pin a take you liked (see voice.json "seed_used"); None = random
ELEVEN_TIMEOUT = 240  # seconds


def voice_backend() -> str:
    load_env()
    return env_choice("VOICE_BACKEND")


def gemini_tts(text: str) -> bytes:
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
        sys.exit(f"no audio returned. Model said: {r.output_text!r}")
    return base64.b64decode(r.output_audio.data)


def eleven_tts(text: str, seed: int) -> tuple[bytes, list[list]]:
    """(WAV bytes, [char, start, end] per character of text)."""
    key = os.environ.get("ELEVENLABS_API_KEY")
    if not key:
        sys.exit("ELEVENLABS_API_KEY missing (put it in .env)")
    body = {"text": text, "model_id": ELEVEN_MODEL, "seed": seed}
    if ELEVEN_SETTINGS:
        body["voice_settings"] = ELEVEN_SETTINGS
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE}/with-timestamps?output_format=wav_24000",
        data=json.dumps(body).encode(), method="POST",
        headers={"xi-api-key": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=ELEVEN_TIMEOUT) as resp:
            r = json.load(resp)
    except urllib.error.HTTPError as e:
        sys.exit(f"ElevenLabs error {e.code}: {e.read().decode(errors='replace')[:500]}")
    a = r.get("alignment") or {}
    chars = [list(x) for x in zip(a.get("characters", []), a.get("character_start_times_seconds", []),
                                  a.get("character_end_times_seconds", []))]
    return base64.b64decode(r["audio_base64"]), chars


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("note", help="note ID, e.g. 001")
    ap.add_argument("--retake", action="store_true", help="re-record even if nothing changed")
    args = ap.parse_args()

    backend = voice_backend()
    title, lines = read_note(args.note)
    text = script_text(lines)
    outdir = OUT / args.note
    outdir.mkdir(parents=True, exist_ok=True)
    if backend == "elevenlabs":
        made_from = {"text": text, "backend": backend, "voice": ELEVEN_VOICE, "model": ELEVEN_MODEL,
                     "settings": ELEVEN_SETTINGS, "seed": ELEVEN_SEED}
    else:
        made_from = {"text": text, "backend": backend, "voice": VOICE, "style": STYLE, "model": TTS_MODEL}
    print(f"{args.note}: {title!r}, {len(lines)} lines, {len(text.split())} words, "
          f"{backend} voice {made_from['voice']}")

    path, manifest_path, words_path = outdir / "voice.wav", outdir / "voice.json", outdir / "words.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    if path.exists() and not args.retake and {k: v for k, v in manifest.items() if k != "seed_used"} == made_from:
        print(f"{path.name}: up to date, skipping" + (f" (seed {manifest['seed_used']})" if "seed_used" in manifest else ""))
        return
    if args.retake and backend == "elevenlabs" and ELEVEN_SEED is not None:
        print(f"note: ELEVEN_SEED is pinned to {ELEVEN_SEED}, so a retake will likely sound the same")
    print(f"{path.name} ({'changed' if path.exists() else 'new'}): recording the whole script...")
    if backend == "elevenlabs":
        seed = ELEVEN_SEED if ELEVEN_SEED is not None else random.randrange(2**32)
        print(f"seed {seed}")
        made_from = {**made_from, "seed_used": seed}
        audio, chars = eleven_tts(text, seed)
        path.write_bytes(audio)
        if chars:  # written after the WAV, so align.py sees it as up to date
            words_path.write_text(json.dumps(script_timings(script_words(lines), chars)))
            print(f"{words_path.name}: word timings from ElevenLabs")
    else:
        path.write_bytes(gemini_tts(text))
    manifest_path.write_text(json.dumps(made_from, indent=2))
    print(f"done: {path}")


if __name__ == "__main__":
    run_cli(main)
