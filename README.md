# Might Make Sense

## Notes site

Notes live in `notes/`:

- `notes/index.md` — the home page, plain Markdown, rendered to `wiki_html/index.html`.
- `notes/NNN.txt` — one note per file (`000`–`999`), following `notes/templates/note.txt`, rendered to `wiki_html/NNN.html`.

`build.py` is a small, dependency-free Python script that renders them into `wiki_html/`.

### Note format

Each note is a record of `Field: value` lines. Lines starting with `#` are comments and are ignored, so the template's guidance can stay in the file.

### Linking

Link between pages with `[[001]]` (matches a note's ID, or its title case-insensitively) or `[[001|display text]]` for custom link text. `Related:` IDs are linked automatically. Unresolved links and IDs are reported as warnings on stderr, and the build still finishes.

Regenerate the site:

```sh
just build
```

Or watch `notes/` and `config.toml` and rebuild on every save, serving `wiki_html/` with live reload in the browser:

```sh
just build-live
```

Both are plain `python3` under the hood (`build.py` and `watch.py`); `just --list` shows all available commands.

Styling lives in `wiki_html/style.css` (black background, white monospace type, one centred column, justified note text) and the page shell lives in `notes/templates/page.html`, which wraps the rendered content in a `<main>`. Both are hand-edited and untouched by `build.py`.

Setup (needs Python 3.11+, since `build.py` uses `tomllib`):

```sh
just setup
```

This creates a `.venv/`, installs `requirements.txt` into it, and copies `.env.example` to `.env` if there isn't one yet. Then fill in `GOOGLE_API_KEY` in `.env` (git-ignored). It's safe to re-run, e.g. after `requirements.txt` changes. `just animate` uses `.venv/bin/python` directly, so there's no need to activate the venv; if it ever gets into a bad state, delete `.venv/` and run `just setup` again.

## Deployment

The site is hosted on **Cloudflare Pages** and served at `mightmakesense.xyz`, with DNS managed by Cloudflare (domain registered at Porkbun).

- **Registrar:** Porkbun — nameservers point to Cloudflare, so all DNS is managed in the Cloudflare dashboard, not Porkbun's.
- **Hosting:** Cloudflare Pages project, connected to this GitHub repo. No build command — Pages just serves the `wiki_html/` directory as-is (the site is generated locally with `build.py` and committed).
    - Build command: _(none)_
    - Build output directory: `wiki_html`
- **Production:** pushes to `main` deploy to `mightmakesense.xyz`.
- **Dev/preview:** other branches (e.g. `dev`) auto-deploy to their own preview URL. To use `dev.mightmakesense.xyz`, map that branch under the Pages project's Custom domains settings (assign the subdomain to the branch, not production).

### Publishing a change

```sh
# after editing notes in notes/
just build
git add notes wiki_html
git commit -m "..."
git push
```

Cloudflare Pages picks up the push automatically and redeploys.

## TODO

1. Rendering options, behind a config file. e.g. Do I want notes to be justified. Note, this isn't a user exposed option. It's a 'build' option.
2. UI improvements, e.g. grid vs table on main index page.
3. Do an LLM semantic relevance embedding. Then some kind of visualisation. (Requires writing a lot more first...)
