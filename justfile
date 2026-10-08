# List available commands
default:
    @just --list

# Create .venv, install requirements, and create .env from the example (safe to re-run)
setup:
    test -d .venv || python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    test -f .env || cp .env.example .env
    @echo "Done. Fill in API keys in .env if you haven't already."

# Build the site once
build:
    python3 build.py

# Rebuild on every change and serve wiki_html/ with live reload
build-live port="8000":
    python3 watch.py {{port}}

# Generate stick-figure frames for one note, e.g. `just animate 001 --dry-run`
animate note *args:
    .venv/bin/python animate/animate.py {{note}} {{args}}
