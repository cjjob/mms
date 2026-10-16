# List available commands
default:
    @just --list

# Create .venv, install requirements, and create .env from the example (safe to re-run)
setup:
    test -d .venv || python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    test -f .env || cp .env.example .env
    @command -v ffmpeg >/dev/null || echo "ffmpeg not found (needed by just stitch): brew install ffmpeg-full"
    @ffmpeg -hide_banner -filters 2>/dev/null | grep -q " ass " || echo "ffmpeg lacks libass (needed for captions): brew install ffmpeg-full && brew unlink ffmpeg && brew link --force --overwrite ffmpeg-full"
    @echo "Done. Fill in API keys in .env if you haven't already."

# One-time setup for IMAGE_BACKEND=local: mflux + FLUX.2 klein 4B saved at 4-bit (~15GB download once)
setup-local:
    .venv/bin/pip install -r requirements-local.txt
    test -d animate/models/flux2-klein-4b-q4 || .venv/bin/mflux-save --model flux2-klein-4b --quantize 4 --path animate/models/flux2-klein-4b-q4
    @echo "Done. Set IMAGE_BACKEND=local in .env (or prefix a command with it) to use it."

# Build the site once
build:
    python3 build.py

# Rebuild on every change and serve wiki_html/ with live reload
build-live port="8000":
    python3 watch.py {{port}}

# Preview the storyboard (cast + frames: where each cut falls and its scene) without generating images; --replan to redo it
storyboard note *args:
    .venv/bin/python animate/animate.py {{note}} --storyboard-only {{args}}

# Generate stick-figure frames for one note, e.g. `just animate 001 --dry-run`
animate note *args:
    .venv/bin/python animate/animate.py {{note}} {{args}}

# Voice the whole script in one clip (Gemini TTS, or ElevenLabs with VOICE_BACKEND=elevenlabs)
voice note *args:
    .venv/bin/python animate/voice.py {{note}} {{args}}

# Voice the script (if it changed) and play it: a cheap preview before drawing anything
listen note: (voice note)
    afplay animate/out/{{note}}/voice.wav

# Word timings for captions (forced alignment with stable-ts; downloads a ~150MB model on first run)
align note *args:
    .venv/bin/python animate/align.py {{note}} {{args}}

# Stitch frames (+ voiceover, if generated) into animate/out/<id>.mp4 with burned-in captions
stitch note *args:
    .venv/bin/python animate/stitch.py {{note}} {{args}}

# Whole pipeline for one note: frames, voiceover, word timings, MP4 (each step skips work already done)
video note: (animate note) (voice note) (align note) (stitch note)

# Change frames in plain English, e.g. `just edit 001 "make the trophy in frame 6 black and white"`; shows a plan and asks first
edit note request *args:
    .venv/bin/python animate/edit.py {{note}} {{quote(request)}} {{args}}

# Delete the run logs in logs/ (every animate/voice/align/stitch/edit run writes one)
clean-logs:
    rm -rf logs

# Change one frame directly, e.g. `just frame 001 6 "make the trophy black and white"` (add --redraw to draw it fresh)
frame note n request *args:
    .venv/bin/python animate/edit.py {{note}} {{quote(request)}} --frame {{n}} {{args}}
