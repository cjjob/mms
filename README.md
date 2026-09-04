# Might Make Sense

## Vimwiki

Notes live in `wiki/`. Vimwiki generates a static HTML version into `wiki_html/`.

Open the generated site locally:

```sh
open wiki_html/index.html
```

## Deployment

The site is hosted on **Cloudflare Pages** and served at `mightmakesense.xyz`, with DNS managed by Cloudflare (domain registered at Porkbun).

- **Registrar:** Porkbun — nameservers point to Cloudflare, so all DNS is managed in the Cloudflare dashboard, not Porkbun's.
- **Hosting:** Cloudflare Pages project, connected to this GitHub repo. No build command — Pages just serves the `wiki_html/` directory as-is (vimwiki output is generated locally and committed).
  - Build command: *(none)*
  - Build output directory: `wiki_html`
- **Production:** pushes to `main` deploy to `mightmakesense.xyz`.
- **Dev/preview:** other branches (e.g. `dev`) auto-deploy to their own preview URL. To use `dev.mightmakesense.xyz`, map that branch under the Pages project's Custom domains settings (assign the subdomain to the branch, not production).

### Publishing a change

```sh
# after editing notes in wiki/ and regenerating in vim (:VimwikiAll2HTML)
git add wiki wiki_html
git commit -m "Update notes"
git push
```

Cloudflare Pages picks up the push automatically and redeploys.
