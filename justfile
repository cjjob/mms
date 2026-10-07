# List available commands
default:
    @just --list

# Build the site once
build:
    python3 build.py

# Rebuild on every change and serve wiki_html/ with live reload
build-live port="8000":
    python3 watch.py {{port}}
