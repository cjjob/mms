# Prompts

One file per prompt. Edit freely; changes apply on the next run. `{name}` is filled in by
the code; any other braces, like the JSON example in `storyboard.txt`, are sent as written.
An unknown `{name}` prints a warning.

## Shared context (the same in every call)

Every model call gets the same brief, so the storyboard knows how its scenes will be drawn and
the drawing knows where its scene came from:

| File | What it is | Used in |
|---|---|---|
| `pipeline.txt` | how the videos are made, step by step, and the frame layout | `{pipeline}`: all prompts |
| `series.txt` | the series bible: purpose, tone, the fixed main character, continuity rules | `{series}`: all but `edit_plan` |
| `style.txt` | the drawing style | `{style}`: all but `edit_plan` |
| `references.txt` | the series reference images (`<name>: <role>` per line, file `animate/<name>.jpg`) | sent first, labelled, with every setting, frame, edit and check call (not the storyboard: shown images, it copies their content). A loose style guide, not a template |

Change these to change the look of the whole series; leave them alone between episodes to keep
it consistent. The layout numbers (`{safe_top_pct}`, `{caption_top_pct}`, `{caption_bottom_pct}`,
`{safe_left_pct}`, `{safe_right_pct}`, `{max_zoom_pct}`) come from the constants at the top of
`animate.py`, which also place the captions (`stitch.py`) and drive the checks (`qa.py`), and are
available in every prompt.

## Per-call prompts

| File | Model call | Placeholders |
|---|---|---|
| `storyboard.txt` | text model: plan cast, settings, frames and camera moves (`just storyboard`) | `title` `script` `n_words` `seconds` `last_word` |
| `setting.txt` | image model: draw each setting once, empty, as its reference | `place` `look` `background` |
| `frame.txt` | image model: draw one frame (`just animate`, edit redraws) | `frame` `n_frames` `cast` `setting` `script` `previous` `words` `scene` `continuity` `camera` `buildup` `next` |
| `build.txt` | image model: draw a build frame, the previous frame plus one element | `frame` `prev` `n_frames` `cast` `adds` `later` `script` `words` `scene` |
| `judge.txt` | judge model: pass/fail checklist for one frame (`qa.py`) | `frame` `n_frames` `cast` `present` `words` `scene` |
| `edit_image.txt` | image model: targeted edit of a frame (`just edit`, qa fixes) | `frame` `n_frames` `instruction` `words` `scene` |
| `edit_plan.txt` | text model: turn a `just edit` request into per-frame changes | `request` `n_frames` `frames` `default_zoom` `default_zoom_pct` |
| `voice_style.txt` | TTS delivery style (changing it re-records every clip) | — |

A few context lines are built in code because they depend on the frame: the image labels
(`frame_images`), `{setting}` (`setting_block`), `{previous}` / `{next}` (the neighbouring
frames' words and scenes), `{continuity}`, `{camera}` (`camera_note`), `{buildup}` (what the rest of a build group will add,
so the first frame leaves room) and `{later}` (the same, for a build frame), all in `animate.py`.
Each frame-NN.txt in the output folder records the exact images (in order) and prompt sent.
