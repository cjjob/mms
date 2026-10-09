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

## Animation

`animate/` turns one note into a short vertical video (1080x1920, for TikTok/Reels): black-and-white stick-figure images, one per line of the note's `Description`, with a voiceover and TikTok-style captions, stitched into an MP4. Fully automatic, no manual editing. It uses Google's Gemini API with the cheapest models, plus ffmpeg:

| Step | Script | Model | Cost (15-line note) |
|---|---|---|---|
| Storyboard | `animate.py --storyboard-only` | `gemini-3.1-flash-lite` | ~free |
| Frames | `animate.py` | `gemini-3.1-flash-lite-image` | ~$0.034/image, ~$0.50 |
| Voiceover | `voice.py` | `gemini-3.8-flash-lite-tts` | ~1p |
| Word timings | `align.py` | Whisper `base.en` via stable-ts, local | free |
| Stitch | `stitch.py` | ffmpeg, local | free |

Setup (needs Python 3.11+, since `build.py` uses `tomllib`, and ffmpeg with libass: `brew install ffmpeg-full`. Homebrew's plain `ffmpeg` is a lite build that can't burn in captions; if you already have it, `brew unlink ffmpeg && brew link --force --overwrite ffmpeg-full`):

```sh
just setup
```

This creates a `.venv/`, installs `requirements.txt` into it, copies `.env.example` to `.env` if there isn't one yet, and warns if ffmpeg (or its libass support) is missing. Then fill in `GOOGLE_API_KEY` in `.env` (git-ignored; values in `.env` override the shell). It's safe to re-run, e.g. after `requirements.txt` changes. The `just` commands use `.venv/bin/python` directly, so there's no need to activate the venv; if it ever gets into a bad state, delete `.venv/` and run `just setup` again.

Run:

```sh
just storyboard 001            # 1. preview: plan the scenes and print them, no images
just storyboard 001 --replan   #    not happy? throw the plan away and make a new one
just animate 001 --dry-run     # 2. optional: write/print the exact image prompts, no API calls at all
just animate 001               # 3. generate the images, using the saved storyboard
just voice 001                 # 4. generate one voiceover clip per line
just align 001                 # 5. find when each word is spoken, for captions
just stitch 001                # 6. build animate/out/001.mp4 with captions

just video 001                 # or steps 3-6 in one go
```

Fixing individual frames afterwards:

```sh
just edit 001 "make the trophy in frame 6 black and white, and reduce the zoom on frame 9"
just frame 001 6 "make the trophy black and white"            # direct: skip the interpreting step
just frame 001 6 "show him at the top of a mountain" --redraw  # draw it fresh instead of editing
```

`just edit` has a cheap text model turn the request into specific per-frame changes, prints them, and asks before applying (`--yes` to skip). Only the named frames change. Later frames, the voice and the word timings are never touched, and the MP4 is rebuilt at the end:

- **zoom / pause**: saved as per-frame overrides in `storyboard.json` (`"overrides": {"9": {"zoom": 0.025}}`). Free. Negative zoom zooms out.
- **image edit** (default): the current frame plus "change only X", with the style reference and neighbouring frames for context. Keeps the composition. ~$0.034.
- **redraw**: for a substantially different scene. Updates the storyboard scene and draws the frame fresh, using the frames before *and* after it as references. ~$0.034.

Replaced frames go to `<id>/history/frame-NN.vN.*`; to undo, copy one back over `frame-NN.*` and run `just stitch`.

The storyboard step is the cheap one to iterate on: check that every line has a sensible, drawable scene before paying for images. `just animate` reuses the saved storyboard, and creates one first if there isn't one yet. Every step skips work that's already done, so re-running is cheap.

How it keeps frames coherent:

1. **Storyboard** — one text-model call reads the whole script and returns a cast description plus one concrete, drawable scene per line. Saved as `storyboard.json` and reused on later runs; `just storyboard <id> --replan` makes a fresh one.
2. **Frames** — each image request gets the style reference `animate/animate_reference.png`, the previous 2 frames, the full script with the current line marked, the lines before and after, and that line's scene.

Stitching: each frame is shown for the length of its voice clip plus a 0.4s pause (3s of silence if there's no clip yet, so you can preview timing before generating audio), with a slow 5% zoom-in ("Ken Burns"), then the segments are joined into one MP4.

Captions: a few words at a time (max 5 words / 24 characters, breaking after punctuation), white Arial Black with a black outline, the spoken word highlighted yellow with a small pop. They sit about a quarter of the way up the frame, clear of TikTok's buttons. They're written as an ASS subtitle file (`<id>.ass`) and burned in with ffmpeg/libass. Word timings come from `just align`: since we already know the exact words, Whisper *aligns* them to the audio rather than transcribing (so it can't mishear). Without alignment, `stitch` estimates timings from each clip's length, which is close but drifts on long lines. stable-ts is in `requirements.txt` (it pulls in PyTorch, ~1GB, so `just setup` takes a while), and `just align` downloads a ~150MB model once. Font, colours, size, position and chunking are constants at the top of `stitch.py`; to use a custom font, drop the `.ttf` into `animate/fonts/` and set `FONT` to its name.

Output goes to `animate/out/` (git-ignored):

- `<id>/storyboard.json` — the shot plan, plus any per-frame zoom/pause overrides
- `<id>/history/` — earlier versions of frames replaced by `just edit` / `just frame`
- `<id>/frame-NN.jpg` (or `.png`) + `frame-NN.txt` — each image and the exact prompt used for it
- `<id>/voice-NN.wav` — the voiceover for line NN
- `<id>/words-NN.json` — word timings for line NN (`[word, start, end]`)
- `<id>.ass` — the captions that were burned in
- `<id>.mp4` — the finished video

To redo something, delete those files and re-run: e.g. delete `frame-05.*` and later to regenerate frames from 5 onwards (each frame builds on the previous ones), or `voice-03.wav` and `words-03.json` to re-record line 3. `just stitch` always rebuilds the MP4.

Composition: the image prompt asks for the subject in the middle half of the frame, with the lower third left as white space for captions and the top eighth clear for app UI.

Tips: one line in the note = one image and one voice clip, so split long lines for better pacing. Models, voice, delivery style, the number of previous frames fed back in, timing and zoom are constants at the top of each script.

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
4. Animation: crossfades between frames, maybe an ElevenLabs (cloned) voice.
