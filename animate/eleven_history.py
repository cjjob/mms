#!/usr/bin/env python3
"""Show the model and settings of your recent ElevenLabs generations (website included).

Usage:
  python3 animate/eleven_history.py [N]

To make `just voice` sound like something you generated on the ElevenLabs
website, find it here and copy its model into ELEVEN_MODEL and its settings into
ELEVEN_SETTINGS in voice.py. Only generations with voice.py's ELEVEN_VOICE are listed.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from animate import load_env, run_cli
from voice import ELEVEN_MODEL, ELEVEN_SETTINGS, ELEVEN_VOICE


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("n", nargs="?", type=int, default=5, help="how many to show (default 5)")
    args = ap.parse_args()
    load_env()
    key = os.environ.get("ELEVENLABS_API_KEY") or sys.exit("ELEVENLABS_API_KEY missing (put it in .env)")
    q = urllib.parse.urlencode({"page_size": args.n, "voice_id": ELEVEN_VOICE})
    req = urllib.request.Request(f"https://api.elevenlabs.io/v1/history?{q}", headers={"xi-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            items = json.load(resp).get("history", [])
    except urllib.error.HTTPError as e:
        sys.exit(f"ElevenLabs error {e.code}: {e.read().decode(errors='replace')[:500]}")
    print(f"voice.py now uses: model {ELEVEN_MODEL}, settings {ELEVEN_SETTINGS or '(the voice defaults)'}\n")
    if not items:
        print(f"no generations found for voice {ELEVEN_VOICE}")
    for it in items:
        when = datetime.fromtimestamp(it.get("date_unix", 0)).strftime("%Y-%m-%d %H:%M")
        text = (it.get("text") or "").replace("\n", " ")
        print(f"{when}  {it.get('source', '?')}  {text[:70]!r}")
        print(f"  ELEVEN_MODEL = {it.get('model_id')!r}")
        print(f"  ELEVEN_SETTINGS = {json.dumps(it.get('settings') or {})}\n")


if __name__ == "__main__":
    run_cli(main)
