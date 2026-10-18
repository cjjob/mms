# Prompts

One file per prompt. Edit freely; changes apply on the next run. `{name}` is filled in by
the code (only the names listed below); any other braces, like the JSON example in
`storyboard.txt`, are sent as written. An unknown `{name}` prints a warning.

| File | Model call | Placeholders |
|---|---|---|
| `storyboard.txt` | text model: plan cast, settings, frames and camera moves (`just storyboard`) | `title` `script` `n_words` `seconds` `last_word` |
| `frame.txt` | image model: draw one frame (`just animate`, edit redraws) | `frame` `n_frames` `style` `cast` `setting` `images` `script` `previous_words` `words` `next_words` `scene` |
| `edit_plan.txt` | text model: turn a `just edit` request into per-frame changes | `request` `n_frames` `frames` `default_zoom` `default_zoom_pct` |
| `edit_image.txt` | image model: targeted edit of an existing frame | `frame` `n_frames` `instruction` `style` `images` `words` `scene` |
| `voice_style.txt` | TTS delivery style (changing it re-records every clip) | — |

Shared pieces, inserted as `{style}`:
- `style.txt` — the drawing style, in `frame.txt` and `edit_image.txt`

A few small context sentences are still built in code because they depend on the frame:
the `{images}` description (which reference image is which), the `{setting}` block, and
`{previous_words}` / `{next_words}` (empty on the first/last frame). See `frame_prompt`
and `setting_block` in `animate.py`.
