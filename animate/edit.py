#!/usr/bin/env python3
"""Change specific frames of a finished video, then rebuild the MP4.

Usage:
  python3 animate/edit.py 001 "make the trophy in frame 6 black and white, and reduce the zoom on frame 9"
  python3 animate/edit.py 001 "make the trophy black and white" --frame 6            # direct, no interpreting
  python3 animate/edit.py 001 "show him at the top of a mountain" --frame 6 --redraw
  add --yes to skip the confirmation

1. Plan: a cheap text model turns the request into specific changes per frame
   (unless --frame is given). The plan is printed and you confirm it.
2. Apply, touching only those frames:
   - zoom / pause  -> stored as per-frame overrides in storyboard.json (no image cost)
   - image "edit"  -> the current frame + the instruction ("change only X"), with the
                      style reference and neighbouring frames for context
   - image "redraw"-> the storyboard scene is updated and the frame is drawn fresh,
                      with the frames before AND after it as references
   Replaced frames are kept in <id>/history/ (frame-06.v1.jpg, ...). Later frames,
   voice and word timings are never touched.
3. Rebuild the MP4 (local, ~30s).
"""

import argparse
import json
import shutil
import sys

from animate import (
    IMAGE_COST, OUT, PLAN_MODEL, PREV_FRAMES, REFERENCE, STYLE,
    find_frame, frame_prompt, generate_image, get_client, load_board, read_note, run_cli, save_board,
)
from stitch import PAUSE, ZOOM, settings, stitch


def plan_changes(request: str, lines: list[str], board: dict) -> list[dict]:
    rows = []
    for i, (line, scene) in enumerate(zip(lines, board["scenes"]), 1):
        zoom, pause = settings(board, i)
        rows.append(f'{i:02d}. line: "{line}"\n    scene: {scene}\n    zoom: {zoom:g}, pause: {pause:g}')
    prompt = f"""You turn a request for changes to a short narrated stick-figure video into a precise
JSON edit plan. The video has {len(lines)} frames, one per narration line. Current state:

{chr(10).join(rows)}

Settings you can change per frame:
- zoom: how far the frame slowly zooms in over its duration. Default {ZOOM:g} (= {ZOOM:.0%}).
  0 = still; negative = zoom out. "reduce the zoom" = roughly halve it; "remove" = 0;
  "more zoom" = roughly double it.
- pause: seconds of silence after the line is spoken. Default {PAUSE:g}.

Image changes:
- "edit": a targeted change to the existing image (change, add, remove or restyle an
  object; change an expression or pose). Keeps the composition. Prefer this.
- "redraw": only when the request needs a substantially different scene or composition.
  Then also give the new "scene" description.
Images must stay black-and-white stick-figure line art with no text.

Request: \"\"\"{request}\"\"\"

Return ONLY JSON:
{{"changes": [{{"frame": 6, "image": "edit" or "redraw" or null,
  "instruction": "precise instruction for the image model, or null",
  "scene": "new scene description (redraw only), or null",
  "zoom": number or null, "pause": number or null}}]}}
Only include frames the request is about; use null for anything not changing. Frame
numbers refer to the numbering above. Never change the narration."""
    r = get_client().interactions.create(model=PLAN_MODEL, input=prompt)
    text = r.output_text or ""
    try:
        return json.loads(text[text.index("{") : text.rindex("}") + 1])["changes"]
    except (ValueError, KeyError, TypeError):
        sys.exit(f"couldn't understand the plan the model returned:\n{text}")


def validate(changes: list[dict], n: int) -> list[dict]:
    out = []
    for c in changes:
        try:
            i = int(c.get("frame"))
        except (TypeError, ValueError):
            continue
        if not 1 <= i <= n:
            print(f"ignoring frame {i}: there are only {n} frames", file=sys.stderr)
            continue
        image = c.get("image") if c.get("image") in ("edit", "redraw") else None
        if image and not (c.get("instruction") or c.get("scene")):
            image = None
        zoom, pause = c.get("zoom"), c.get("pause")
        zoom = max(-0.3, min(0.3, float(zoom))) if isinstance(zoom, (int, float)) else None
        pause = max(0.0, min(5.0, float(pause))) if isinstance(pause, (int, float)) else None
        if image or zoom is not None or pause is not None:
            out.append({"frame": i, "image": image, "instruction": c.get("instruction"),
                        "scene": c.get("scene") if image == "redraw" else None, "zoom": zoom, "pause": pause})
    return out


def describe(changes: list[dict], board: dict) -> str:
    rows = []
    for c in changes:
        i = c["frame"]
        zoom, pause = settings(board, i)
        if c["image"] == "edit":
            rows.append(f"frame {i:02d}  image edit: {c['instruction']}")
        if c["image"] == "redraw":
            rows.append(f"frame {i:02d}  redraw: {c['scene'] or c['instruction']}")
            if c["scene"] and c["instruction"]:
                rows.append(f"           also: {c['instruction']}")
        if c["zoom"] is not None:
            rows.append(f"frame {i:02d}  zoom {zoom:g} -> {c['zoom']:g}")
        if c["pause"] is not None:
            rows.append(f"frame {i:02d}  pause {pause:g}s -> {c['pause']:g}s")
    return "\n".join(rows)


def backup(outdir, i: int) -> None:
    """Move frame-NN.* (image + prompt) into history/ as the next version."""
    hist = outdir / "history"
    hist.mkdir(exist_ok=True)
    v = 1
    while list(hist.glob(f"frame-{i:02d}.v{v}.*")):
        v += 1
    img = find_frame(outdir, i)
    if img:
        img.rename(hist / f"frame-{i:02d}.v{v}{img.suffix}")
    txt = outdir / f"frame-{i:02d}.txt"
    if txt.exists():
        txt.rename(hist / f"frame-{i:02d}.v{v}.txt")


def edit_prompt(i: int, lines: list[str], board: dict, instruction: str, n_prev: int, n_next: int) -> str:
    refs = "Image 2 is the style and main-character reference."
    if n_prev or n_next:
        refs += f" Images 3-{2 + n_prev + n_next} are the neighbouring frames, in story order, for continuity."
    return f"""You are editing frame {i} of {len(lines)} of a short narrated stick-figure video.

IMAGE 1 is the current frame. Apply ONLY this change:
{instruction}

Keep everything else exactly as it is: composition, framing, characters, poses, expressions,
props, line weight and style. Output the complete edited frame.

STYLE (the result must still match it): {STYLE}

OTHER IMAGES (reference only, do not copy them): {refs}

This frame's narration: "{lines[i - 1]}"
Scene: {board['scenes'][i - 1]}"""


def apply(changes: list[dict], note: str, lines: list[str], board: dict) -> None:
    outdir = OUT / note
    overrides = board.setdefault("overrides", {})
    for c in changes:
        i = c["frame"]
        ov = overrides.setdefault(str(i), {})
        if c["zoom"] is not None:
            ov["zoom"] = c["zoom"]
        if c["pause"] is not None:
            ov["pause"] = c["pause"]
        if not ov:
            del overrides[str(i)]
    save_board(outdir, board)  # settings saved even if an image call fails below

    for c in changes:
        if not c["image"]:
            continue
        i, stem = c["frame"], f"frame-{c['frame']:02d}"
        prev = [p for k in range(max(1, i - PREV_FRAMES), i) if (p := find_frame(outdir, k))]
        nxt = [p for p in [find_frame(outdir, i + 1)] if p]
        current = find_frame(outdir, i)

        if c["image"] == "edit" and current:
            prompt = edit_prompt(i, lines, board, c["instruction"], len(prev), len(nxt))
            refs = [current, REFERENCE, *prev, *nxt]
        else:  # redraw (or edit with no current image)
            if c["scene"]:
                board["scenes"][i - 1] = c["scene"]
                save_board(outdir, board)
            prompt = frame_prompt(i, lines, board, len(prev), len(nxt))
            if c["instruction"]:
                prompt += f"\n\nSPECIFIC REQUEST FOR THIS FRAME: {c['instruction']}"
            refs = [REFERENCE, *prev, *nxt]

        print(f"{stem}: {c['image']}...")
        # Generate to a temporary name first, so a failed call leaves the old frame in place.
        new = generate_image(prompt, refs, outdir, f"{stem}.new")
        backup(outdir, i)
        new.rename(outdir / f"{stem}{new.suffix}")
        (outdir / f"{stem}.txt").write_text(prompt)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("note", help="note ID, e.g. 001")
    ap.add_argument("request", help="what to change, in plain English")
    ap.add_argument("--frame", type=int, help="apply the request to this frame directly (no interpreting)")
    ap.add_argument("--redraw", action="store_true", help="with --frame: redraw from scratch instead of editing")
    ap.add_argument("--yes", "-y", action="store_true", help="don't ask for confirmation")
    args = ap.parse_args()

    _, lines = read_note(args.note)
    outdir = OUT / args.note
    board = load_board(outdir)
    if board is None or not find_frame(outdir, 1):
        sys.exit(f"nothing to edit yet; run `just video {args.note}` first")

    if args.frame:
        raw = [{"frame": args.frame, "image": "redraw" if args.redraw else "edit",
                "instruction": args.request, "scene": None}]
    else:
        print("planning...")
        raw = plan_changes(args.request, lines, board)
    changes = validate(raw, len(lines))
    if not changes:
        sys.exit("no changes to make")

    n_images = sum(1 for c in changes if c["image"])
    print(f"\n{describe(changes, board)}\n")
    cost = f" (~${n_images * IMAGE_COST:.2f})" if n_images else ""
    if not args.yes and input(f"apply{cost}? [y/N] ").strip().lower() not in ("y", "yes"):
        sys.exit("cancelled")

    apply(changes, args.note, lines, board)
    stitch(args.note)


if __name__ == "__main__":
    run_cli(main)
