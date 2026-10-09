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

# Build the site once
build:
    python3 build.py

# Rebuild on every change and serve wiki_html/ with live reload
build-live port="8000":
    python3 watch.py {{port}}

# Preview the storyboard (cast + one scene per line) without generating images; --replan to redo it
storyboard note *args:
    .venv/bin/python animate/animate.py {{note}} --storyboard-only {{args}}

# Generate stick-figure frames for one note, e.g. `just animate 001 --dry-run`
animate note *args:
    .venv/bin/python animate/animate.py {{note}} {{args}}

# Generate a voiceover clip per line with Gemini TTS
voice note *args:
    .venv/bin/python animate/voice.py {{note}} {{args}}

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

# Change one frame directly, e.g. `just frame 001 6 "make the trophy black and white"` (add --redraw to draw it fresh)
frame note n request *args:
    .venv/bin/python animate/edit.py {{note}} {{quote(request)}} --frame {{n}} {{args}}
