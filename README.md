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
python3 build.py
```

Open it locally:

```sh
open wiki_html/index.html
```

Styling lives in `wiki_html/style.css` (black background, white monospace type, one centred column, justified note text) and the page shell lives in `notes/templates/page.html`, which wraps the rendered content in a `<main>`. Both are hand-edited and untouched by `build.py`.

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
python3 build.py
git add notes wiki_html
git commit -m "..."
git push
```

Cloudflare Pages picks up the push automatically and redeploys.
