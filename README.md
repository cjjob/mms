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
| Frames | `animate.py` | `image_model` in `config.toml` (default `gemini-nano-banana-2.1`), checked by `judge_model` | ~$0.045/frame + one image per setting, plus redraws of frames that fail the check |
| Thumbnail | `thumbnail.py` (run by `animate.py`) | `image_model` for the picture; the title is set with Pillow | ~$0.045 (twice if the picture strays into the title band) |
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
just video 001 --force         #   new storyboard and new frames (old ones to history/); keeps the voice
just video 001 --no-thumbnail  #   without the cover thumbnail
just thumbnail 001 --redraw    # a new cover picture (the title is re-set on every run, free)
```

The steps form two independent branches off the note, which only meet at `stitch`. So the voice can be drafted before, after or alongside the storyboard and frames (read top to bottom):

```
*   note          notes/001.txt                   edit it, then rerun either branch
|\
| * voice         -> voice.wav, voice.json        `just voice` / `just listen`
| * align         -> words.json                   needs voice.wav
* | storyboard    -> storyboard.json              `just storyboard`
* | frames        -> frame-NN.png                 `just animate`
* | thumbnail     -> 001-thumbnail.png            `just animate` / `just thumbnail`
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
- **image edit** (default): the current frame plus "change only X", with the series references, setting reference and previous frame for context. Keeps the composition; the result is checked and the verdict printed. ~$0.045.
- **rebuild** (automatic): changing a frame's image redraws the build frames that add to it, on top of the new version, so a group never mixes old and new. They're listed in the plan, with the cost, before you confirm. A zoom change on any frame of a build group changes the whole group's camera.
- **redraw**: for a substantially different scene. Updates the storyboard scene and draws the frame fresh, exactly like `just animate`, through the quality check. ~$0.045 per try.

Replaced frames go to `<id>/history/frame-NN.vN.*`; to undo, copy one back over `frame-NN.*` and run `just stitch`.

The storyboard step is the cheap one to iterate on: check the cut points and that every frame has a sensible, drawable scene before paying for images. `just animate` reuses the saved storyboard, and creates one first if there isn't one yet. Every step skips work that's already done, so re-running is cheap.

How it keeps frames coherent:

1. **Storyboard** — one text-model call (`text_model` in `config.toml`) reads the whole script, with every word numbered, plus the same shared brief the drawing model gets (but not the reference images, whose content it would copy) (how the pipeline works, the frame layout, the series bible and the style guide), so it writes scenes that can be drawn well. It decides how many frames there are and where each starts (any word; `MAX_FRAMES = 120` is only a safety limit). It plans a `cast` (name + look; the main character is fixed by `prompts/series.txt`) and `settings` (fixed layout plus small background details), and for each frame: its `setting`, the `cast` in it, a drawable `scene`, what changed since the previous frame (`continuity`), and a `camera` (`move` hold/zoom_in/zoom_out/pan, a `focus` point kept inside the main band, an optional `intensity` 0-1). Before any image is paid for, the plan is checked (unknown cast names, more than 3 characters in a frame, very short scenes, missing camera) and the problems are printed. Saved as `storyboard.json` and reused on later runs; `just storyboard <id> --replan` makes a fresh one and moves the old plan's frames, setting images and checks to `history/before-replan-N/`. Older storyboards still load.
2. **Settings and frames** — each setting is first drawn once, empty (`setting-<name>.png`), as its reference. Then each frame is drawn in its own stateless call, with labelled images first: the series references from `prompts/references.txt`, its setting reference and the previous frame (`PREV_FRAMES = 1`, for poses and props only, so drift can't compound), then the prompt: the shared brief (`pipeline.txt`, `series.txt`, `style.txt`), cast, setting, the full script with this frame's words marked, the previous and next frames' words and scenes, what changed since the previous frame, and where the camera will move.
3. **Build frames** — when the narration lists things or builds an idea up step by step, the storyboard can make a frame `"build": true`: the previous frame plus one new element (`"adds"`), like the next bullet point appearing on a slide. A build group is a normal frame followed by up to 6 build frames, and the storyboard plans the first frame with room for what's coming (its prompt lists the later additions, not to draw yet). A build frame is drawn as an edit of the previous frame (`prompts/build.txt`), then **locked** to it (`buildup.py`): wherever the new drawing has ink with no ink nearby in the previous frame (or the reverse), that area, grown a little and feathered, is the addition; everything else is copied back from the previous frame pixel for pixel, so lines that wobbled in the redraw snap back and nothing already on screen moves. The unlocked drawing is kept as `frame-NN.raw.*` in `history/`. The plan check warns about groups longer than 6 or a group camera that zooms in or pans (it can crop later additions). In the storyboard printout, build frames show as `+ adds to NN`.
4. **Quality check** (`qa.py`) — every new frame gets pixel checks (too much colour, background not white; ink in the caption band and top strip is measured but not enforced) and a judge model's pass/fail checklist against the references and its scene. A failed frame is fixed (an edit for stray writing, otherwise a redraw with the judge's complaint), up to 3 tries; frames that still fail are listed at the end. All tries are kept in `<id>/history/qa/`, results in `<id>/qa.json`. Build frames are also compared with the frame they add to (`buildup.py`): if almost nothing changed (`changed` under `CHANGED_MIN`) it fails as `build_missing`; if much of the existing drawing changed (`moved` over `MOVED_MAX`: the model redrew the frame instead of adding to it) it fails as `build_drift`. Both are redrawn. `just qa <id>` checks existing frames without drawing (use it to tune the thresholds at the top of `qa.py`); `--no-qa` skips it.

Thumbnail (`thumbnail.py`): after the frames, `just animate` (and so `just video`) makes a cover image for the post, `animate/out/<id>-thumbnail.png` (1080x1920); `--no-thumbnail` skips it. It's two steps on purpose. First the image model draws the episode's main theme as one bold visual metaphor (`prompts/thumbnail.txt`: the main character, large, plus one or two big props, lots of white space), from the series references, the storyboard's cast, the title and the whole narration, leaving the top of the image empty. If that top band has more than `TITLE_BAND_INK_MAX` ink it's drawn once more and the cleaner try kept. Then the title is set over the empty band with Pillow, at the largest size that fits, words spread evenly over up to 4 lines.

Thumbnail font: **Fredoka Bold**, on every cover. It's part of the repo, so there's nothing to install: `animate/fonts/Fredoka.ttf` is the variable font from Google Fonts (weights Light to Bold; `FONT_WEIGHT` picks Bold), with its licence, the SIL Open Font License, in `animate/fonts/OFL.txt`, which allows bundling it like this (keep `OFL.txt` next to it). To update it, download `Fredoka[wdth,wght].ttf` from https://github.com/google/fonts/tree/main/ofl/fredoka and save it as `animate/fonts/Fredoka.ttf`. Black text with a 10px white outline (so a stray line of the picture never touches the letters), 70-190px depending on the title's length, as written (not uppercased). To use another font, drop its `.ttf` into `animate/fonts/` and set `FONT` (and `FONT_WEIGHT`, or `None` for a non-variable font) at the top of `thumbnail.py`; colours, outline, size range and `UPPERCASE` are next to it. The captions still use Arial Black (`FONT` in `stitch.py`). The title is never drawn by the model: image models misspell text and never letter it the same way twice. The picture is saved as `<id>/thumbnail-art.*` and reused, and the title is re-set on every run (free), so editing the note's title just needs `just thumbnail <id>`; `--redraw` draws a new picture (the old one goes to `history/`). Layout and type are constants at the top of `thumbnail.py`: the title band is 14-37% down and the picture is asked to fill 41-86% (a gap, since the model tends to draw a little high), both inside the middle 3:4 that profile grids show.

Voiceover: the whole Description is voiced in a single call, so the delivery flows across lines instead of stopping and restarting at each one. Lines in the note don't affect the audio at all. List items (`- Nap`) are voiced and captioned without the dash, with a full stop added if they don't end in punctuation, so the voice pauses between them. Set `VOICE_BACKEND=elevenlabs` in `.env` to use ElevenLabs instead (voice ID, model and optional `voice_settings` are constants at the top of `voice.py`; needs `ELEVENLABS_API_KEY`). ElevenLabs returns per-character timings with the audio, so `voice.py` writes `words.json` itself and `just align` skips. Switching backend re-records. If it sounds different from the ElevenLabs website, the model or settings differ: `just eleven-history` prints the model and settings of your recent generations with that voice (website ones included) to copy into `ELEVEN_MODEL` / `ELEVEN_SETTINGS`. Each ElevenLabs take uses a random seed (printed, and saved in `voice.json` as `seed_used`); `just listen 001 --retake` records a new take, and once you like one, copy its seed into `ELEVEN_SEED` to keep it. On `eleven_v3`/`eleven_v4` you can also direct delivery in the note with audio tags like `[excited]`, ellipses for pauses and CAPS for emphasis (tags currently show up in the captions).

Stitching: the narration is that one clip (or 3s of silence per line before there is one, so you can preview timing before generating audio). Each frame appears as its first word is spoken (from the word timings; estimated if `just align` hasn't run) and stays until the next one, with its storyboard camera move: a crop-and-scale over the frame's duration with ffmpeg zoompan (zoom in on / out from the focus point, or pan towards it; intensity 1 = 25% zoom). A frame and its build frames are one shot: the first frame's camera move runs across the whole group, so the zoom carries on smoothly while elements appear instead of restarting at each one. Frames with no camera, from older storyboards, get the old slow 5% centred zoom-in, and a `just edit` zoom override replaces the move; then video, narration and captions are combined into one MP4.

Captions: a few words at a time (max 5 words / 24 characters, breaking after punctuation), white Arial Black with a black outline, the spoken word highlighted yellow with a small pop. They sit about a quarter of the way up the frame, clear of TikTok's buttons. They're written as an ASS subtitle file (`<id>.ass`) and burned in with ffmpeg/libass. Caption chunks never run across two lines of the note. Word timings come from `just align`: since we already know the exact words, Whisper *aligns* them to the audio rather than transcribing (so it can't mishear). Without alignment, `stitch` estimates timings from the clip's length, which drifts noticeably over a whole script, so always align before posting. stable-ts is in `requirements.txt` (it pulls in PyTorch, ~1GB, so `just setup` takes a while), and `just align` downloads a ~150MB model once. Font, colours, size, position and chunking are constants at the top of `stitch.py`; to use a custom font, drop the `.ttf` into `animate/fonts/` and set `FONT` to its name.

Output goes to `animate/out/` (git-ignored):

- `<id>/storyboard.json` — the shot plan, plus any per-frame zoom overrides
- `<id>/history/` — earlier versions of frames replaced by `just edit` / `just frame`
- `<id>/frame-NN.jpg` (or `.png`) + `frame-NN.txt` — each image and the exact prompt used for it
- `<id>/voice.wav` + `voice.json` — the voiceover for the whole script, and the text/voice/style it was made from
- `<id>/words.json` — timings for every word of the script (`[word, start, end]`)
- `<id>.ass` — the captions that were burned in
- `<id>.mp4` — the finished video
- `<id>-thumbnail.png` — the cover thumbnail, with the title (`<id>/thumbnail-art.*` is the picture alone, `thumbnail-art.txt` its prompt)

The voiceover is matched to the note's text: edit anything in the Description (or the voice settings in `voice.py`, or `VOICE_BACKEND`) and `just voice` re-records the whole script (~1p), `just align` redoes the timings when the clip is newer, and `just stitch` refuses to build with a voiceover that doesn't match the note. To redo a frame, use `just edit` / `just frame`, or delete `frame-NN.*` and re-run `just animate`. `just stitch` always rebuilds the MP4.

Composition: one layout, defined once at the top of `animate.py` and given to every prompt: the top 15% clear for app UI, the main action in the band from 15% to 62% down and 11% to 82% across, captions in the band from 62% to 72% (about y 1190-1380, above the account name and buttons), and plain ground below. These are roughly the TikTok, Reels and Shorts safe zones; the strictest ad specs say 65%, which looked too high.

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
