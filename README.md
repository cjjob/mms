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

`animate/` turns one note into a short vertical video (1080x1920, for TikTok/Reels): black-and-white stick-figure images (the storyboard decides how many and where each cut falls), with a voiceover and TikTok-style captions, stitched into an MP4. Fully automatic, no manual editing. It uses Google's Gemini API with the cheapest models, plus ffmpeg:

| Step | Script | Model | Cost (note 001, ~2 min) |
|---|---|---|---|
| Storyboard | `animate.py --storyboard-only` | `text_model` in `config.toml` (currently `gemini-4-argon`) | a few cents |
| Frames | `animate.py` | `gemini-3.1-flash-lite-image` (or local, see below) | ~$0.034/image; ~40-65 frames, ~$1.35-2.20 |
| Voiceover | `voice.py` | `gemini-3.8-flash-lite-tts` (or ElevenLabs, see below) | ~1p (ElevenLabs: credits per character) |
| Word timings | `align.py` | Whisper `base.en` via stable-ts, local | free |
| Stitch | `stitch.py` | ffmpeg, local | free |

Setup (needs Python 3.11+, since `build.py` uses `tomllib`, and ffmpeg with libass: `brew install ffmpeg-full`. Homebrew's plain `ffmpeg` is a lite build that can't burn in captions; if you already have it, `brew unlink ffmpeg && brew link --force --overwrite ffmpeg-full`):

```sh
just setup
```

This creates a `.venv/`, installs `requirements.txt` into it, copies `.env.example` to `.env` if there isn't one yet, and warns if ffmpeg (or its libass support) is missing. Then fill in `GOOGLE_API_KEY` in `.env` (git-ignored; values in `.env` override the shell). It's safe to re-run, e.g. after `requirements.txt` changes. The `just` commands use `.venv/bin/python` directly, so there's no need to activate the venv; if it ever gets into a bad state, delete `.venv/` and run `just setup` again.

Run:

```sh
# Cheap drafts: edit the note (or storyboard) and repeat until happy
just storyboard 001            # plan the scenes and print them, no images (a few cents)
just storyboard 001 --replan   #   not happy? throw the plan away and make a new one
just listen 001                # voice the whole script and play it (~1p; re-records only if the note changed)

# Expensive: draw, then put it together
just animate 001 --dry-run     # optional: write/print the exact image prompts, no API calls at all
just animate 001               # generate the images, using the saved storyboard
just align 001                 # find when each word is spoken (skipped for ElevenLabs)
just stitch 001                # build animate/out/001.mp4 with captions

just video 001                 # or everything in one go (each step skips work already done)
```

The steps form two independent branches off the note, which only meet at `stitch`. So the voice can be drafted before, after or alongside the storyboard and frames (read top to bottom):

```
*   note          notes/001.txt                   edit it, then rerun either branch
|\
| * voice         -> voice.wav, voice.json        `just voice` / `just listen`
| * align         -> words.json                   needs voice.wav
* | storyboard    -> storyboard.json              `just storyboard`
* | frames        -> frame-NN.png                 `just animate`
|/
*   stitch        -> 001.mp4                      needs both branches
```

Editing the note invalidates both branches: `just voice` re-records, and `just stitch` refuses to build with a voiceover that doesn't match the note. The storyboard's cuts point at word positions and nothing checks them, so redo it (`--replan`) if you added or removed words.

Fixing individual frames afterwards:

```sh
just edit 001 "make the trophy in frame 6 black and white, and reduce the zoom on frame 9"
just frame 001 6 "make the trophy black and white"            # direct: skip the interpreting step
just frame 001 6 "show him at the top of a mountain" --redraw  # draw it fresh instead of editing
```

`just edit` has a cheap text model turn the request into specific per-frame changes, prints them, and asks before applying (`--yes` to skip). Only the named frames change. Later frames, the voice and the word timings are never touched, and the MP4 is rebuilt at the end:

- **zoom**: saved as a per-frame override in `storyboard.json` (`"overrides": {"9": {"zoom": 0.025}}`). Free. Negative zoom zooms out.
- **image edit** (default): the current frame plus "change only X", with the style reference and neighbouring frames for context. Keeps the composition. ~$0.034.
- **redraw**: for a substantially different scene. Updates the storyboard scene and draws the frame fresh, using the frames before *and* after it as references. ~$0.034.

Replaced frames go to `<id>/history/frame-NN.vN.*`; to undo, copy one back over `frame-NN.*` and run `just stitch`.

The storyboard step is the cheap one to iterate on: check the cut points and that every frame has a sensible, drawable scene before paying for images. `just animate` reuses the saved storyboard, and creates one first if there isn't one yet. Every step skips work that's already done, so re-running is cheap.

Running the image model locally:

```sh
just setup-local                     # once: mflux + FLUX.2 klein 4B saved at 4-bit (~15GB download)
IMAGE_BACKEND=local just video 001   # or put IMAGE_BACKEND=local in .env
```

With `IMAGE_BACKEND=local`, frames and edits are drawn on this Mac by **FLUX.2 klein 4B via mflux** (Apache 2.0, 4-bit, 576x1024, 4 steps, fits a 16GB Mac) instead of Gemini. Small local models do better with short prompts, so the local path sends a compact prompt (`LOCAL_STYLE` in `animate.py`) with fewer reference images: the style reference plus the previous frame, or for edits the current frame plus the reference. To set the character's look, replace `animate/animate_reference.png` with a drawing of your own. Expect roughly 20-60s per image on an M-series Air; the first run of each command spends extra time loading the model. If `mflux-save` runs out of memory during setup, skip it: the script falls back to quantizing on load (slower start, same result). The saved model lives in `animate/models/` (git-ignored).

Why text always uses Gemini: the storyboard and `just edit` planning are single text calls costing a few cents at most (~3k tokens in, a few thousand out; model set by `text_model` in `config.toml`). They're also where model quality matters most, since they decide every cut and scene. A local text model (e.g. qwen3:8b via Ollama, which was considered and dropped) would save less than a penny per video, do the job noticeably worse, and add a ~5GB download plus a running server. Images are where the cost is (~$0.034 each, $1+ per video), so that's the only step with a local option. The voiceover also stays on Gemini TTS (~1p per video).

How it keeps frames coherent:

1. **Storyboard** — one text-model call reads the whole script, with every word numbered, and decides how many frames there are and where each one starts. A cut can land on any word; the model picks the count and pacing itself (`MAX_FRAMES = 120` is only a safety limit). The only hard rules in the prompt: simple stick-figure art (the reference image is loose inspiration), line-drawn only (coloured lines fine, no fills), and continuity across the piece. The text model is `text_model` in `config.toml`. Each frame gets a concrete, drawable scene. The model also plans a `cast` and `settings` (fixed layout plus small background details), and gives every frame a `camera`: `move` (hold, zoom_in, zoom_out or pan), a rough `focus` point as fractions of the frame, and an optional `intensity` (0-1). When drawing, each frame gets its setting's description, and the first frame drawn in that setting is sent as an extra reference so the background stays put. Saved as `storyboard.json` (`{"start": word number, "text": the words it covers, "setting": ..., "scene": ..., "camera": {"move": ..., "focus": {"x": ..., "y": ...}, "intensity": ...}}`) and reused on later runs; `just storyboard <id> --replan` makes a fresh one and moves the old plan's frames to `history/before-replan-N/`, since they no longer match. Storyboards from before this change (one frame per line) still load as-is.
2. **Frames** — each image request gets the style reference `animate/animate_reference.png`, the previous `PREV_FRAMES` (4) frames, the full script with this frame's words marked, the previous and next frames' words, and this frame's scene.

Voiceover: the whole Description is voiced in a single call, so the delivery flows across lines instead of stopping and restarting at each one. Lines in the note don't affect the audio at all. Set `VOICE_BACKEND=elevenlabs` in `.env` to use ElevenLabs instead (voice ID, model and optional `voice_settings` are constants at the top of `voice.py`; needs `ELEVENLABS_API_KEY`). ElevenLabs returns per-character timings with the audio, so `voice.py` writes `words.json` itself and `just align` skips. Switching backend re-records. If it sounds different from the ElevenLabs website, the model or settings differ: `just eleven-history` prints the model and settings of your recent generations with that voice (website ones included) to copy into `ELEVEN_MODEL` / `ELEVEN_SETTINGS`. Each ElevenLabs take uses a random seed (printed, and saved in `voice.json` as `seed_used`); `just listen 001 --retake` records a new take, and once you like one, copy its seed into `ELEVEN_SEED` to keep it. On `eleven_v3`/`eleven_v4` you can also direct delivery in the note with audio tags like `[excited]`, ellipses for pauses and CAPS for emphasis (tags currently show up in the captions).

Stitching: the narration is that one clip (or 3s of silence per line before there is one, so you can preview timing before generating audio). Each frame appears as its first word is spoken (from the word timings; estimated if `just align` hasn't run) and stays until the next one, with its storyboard camera move: a crop-and-scale over the frame's duration with ffmpeg zoompan (zoom in on / out from the focus point, or pan towards it; intensity 1 = 25% zoom). Frames with no camera, from older storyboards, get the old slow 5% centred zoom-in, and a `just edit` zoom override replaces the move; then video, narration and captions are combined into one MP4.

Captions: a few words at a time (max 5 words / 24 characters, breaking after punctuation), white Arial Black with a black outline, the spoken word highlighted yellow with a small pop. They sit about a quarter of the way up the frame, clear of TikTok's buttons. They're written as an ASS subtitle file (`<id>.ass`) and burned in with ffmpeg/libass. Caption chunks never run across two lines of the note. Word timings come from `just align`: since we already know the exact words, Whisper *aligns* them to the audio rather than transcribing (so it can't mishear). Without alignment, `stitch` estimates timings from the clip's length, which drifts noticeably over a whole script, so always align before posting. stable-ts is in `requirements.txt` (it pulls in PyTorch, ~1GB, so `just setup` takes a while), and `just align` downloads a ~150MB model once. Font, colours, size, position and chunking are constants at the top of `stitch.py`; to use a custom font, drop the `.ttf` into `animate/fonts/` and set `FONT` to its name.

Output goes to `animate/out/` (git-ignored):

- `<id>/storyboard.json` — the shot plan, plus any per-frame zoom overrides
- `<id>/history/` — earlier versions of frames replaced by `just edit` / `just frame`
- `<id>/frame-NN.jpg` (or `.png`; always `.png` locally) + `frame-NN.txt` — each image and the exact prompt used for it
- `<id>/voice.wav` + `voice.json` — the voiceover for the whole script, and the text/voice/style it was made from
- `<id>/words.json` — timings for every word of the script (`[word, start, end]`)
- `<id>.ass` — the captions that were burned in
- `<id>.mp4` — the finished video

The voiceover is matched to the note's text: edit anything in the Description (or the voice settings in `voice.py`, or `VOICE_BACKEND`) and `just voice` re-records the whole script (~1p), `just align` redoes the timings when the clip is newer, and `just stitch` refuses to build with a voiceover that doesn't match the note. To redo a frame, use `just edit` / `just frame`, or delete `frame-NN.*` and re-run `just animate`. `just stitch` always rebuilds the MP4.

Composition: the image prompt asks for the subject in the middle half of the frame, with the lower third left as white space for captions and the top eighth clear for app UI.

Tips: lines in the note only group the captions (and set silent-preview timing); the storyboard decides the images, and the voice reads straight through. All prompts sent to the models (storyboard, frame drawing, edits, voice delivery style) are plain text files in `animate/prompts/`, one per call; see the README there. Models, voice, the number of previous frames fed back in, timing and zoom are constants at the top of each script.

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
