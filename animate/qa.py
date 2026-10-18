"""Quality gate for frames: catch errant frames before they reach the video.

Each new frame goes through two tiers of checks:
  1. pixel checks (free, instant): too much colour, background not white. Ink in the caption band
     and top UI strip is measured but not enforced (too strict for now). Line art on white makes these simple statistics meaningful.
  2. a judge (JUDGE_MODEL, ~$0.002 a frame): a vision model compares the frame with the series
     references, the setting reference and the previous frame, and answers a fixed checklist
     with true/false per defect. Pass/fail per defect, not a 1-10 score: judges rank and spot
     defects reliably but can't score consistently.
A frame that fails is redrawn, up to MAX_ATTEMPTS in total. A failure that's only stray writing is
fixed with a targeted edit; anything else is redrawn from the clean references, with the judge's
complaint added to the prompt. If every attempt fails, the attempt with the fewest failures is used
and the frame is flagged in the summary (fix it with `just edit`).

Every attempt is kept in out/<id>/history/qa/; results are in out/<id>/qa.json. The pixel
thresholds are first guesses: `just qa <id>` prints the numbers for existing frames, so you can
tune them against frames you've judged by eye.
"""

import json
import shutil
import sys
from pathlib import Path

from animate import (CAPTION_BOTTOM, CAPTION_TOP, JUDGE_MODEL, SAFE_LEFT, SAFE_RIGHT, SAFE_TOP, Images,
                     ask_text, cast_text, find_frame, frame_images, frame_text, generate_image, load_prompt,
                     present_text)

MAX_ATTEMPTS = 3  # draws per frame, including the first

# Pixel thresholds (fractions of pixels). First guesses; tune with `just qa`.
CAPTION_BAND_INK_MAX = 0.04  # dark pixels in the caption band (a ground line is ~1-2%); not enforced
TOP_INK_MAX = 0.02           # dark pixels in the top UI strip; not enforced
COLOUR_MAX = 0.08            # strongly coloured pixels in the whole frame (accents only)
WHITE_MIN = 0.6              # near-white pixels in the whole frame (white background)

CHECKS = ("one_image", "characters_on_model", "style_matches", "original", "wordless", "layout_ok",
          "scene_matches", "continuity_ok")
LOCAL = {"wordless"}  # failures fixed by a targeted edit rather than a redraw
NOT_REDRAWN = {"not_judged"}  # the judge couldn't answer: flagged, but not worth a redraw


def pixel_checks(path: Path) -> tuple[dict, list[str]]:
    """(metrics, failed check names) from simple statistics of the image."""
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        sys.exit("qa needs Pillow and numpy: just setup")
    im = np.asarray(Image.open(path).convert("RGB"), dtype=np.float32) / 255
    h, w, _ = im.shape
    lum = im @ np.array([0.299, 0.587, 0.114], dtype=np.float32)
    ink = lum < 0.5
    hi, lo = im.max(axis=2), im.min(axis=2)
    coloured = ((hi - lo) / (hi + 1e-6) > 0.35) & (hi > 0.25)
    m = {
        "caption_band_ink": float(ink[int(CAPTION_TOP * h):int(CAPTION_BOTTOM * h),
                                      int(SAFE_LEFT * w):int(SAFE_RIGHT * w)].mean()),
        "top_ink": float(ink[:int(SAFE_TOP * h)].mean()),
        "colour": float(coloured.mean()),
        "white": float((lum > 0.9).mean()),
    }
    # caption_band_ink and top_ink are measured (shown by `just qa`) but don't fail a frame for now:
    # too strict. To turn them back on, add them here with CAPTION_BAND_INK_MAX / TOP_INK_MAX.
    failed = [name for name, bad in (("colour_sparing", m["colour"] > COLOUR_MAX),
                                     ("white_background", m["white"] < WHITE_MIN)) if bad]
    return {k: round(v, 4) for k, v in m.items()}, failed


def judge(board: dict, lines: list[str], i: int, images: Images, frame: Path) -> dict:
    """The judge's checklist for frame i: {check: bool, ..., "problems": str}; {} if unusable."""
    prompt = load_prompt("judge", frame=i, n_frames=len(board["frames"]), cast=cast_text(board),
                         present=present_text(board, i), words=frame_text(board, lines, i),
                         scene=board["frames"][i - 1]["scene"])
    for _ in range(2):  # one retry if the reply isn't the JSON asked for
        text = ask_text(prompt, [*images, ("FRAME TO CHECK", frame)], model=JUDGE_MODEL)
        try:
            out = json.loads(text[text.index("{"): text.rindex("}") + 1])
            return {**{c: out[c] is not False for c in CHECKS}, "problems": str(out.get("problems") or "")}
        except (ValueError, KeyError, TypeError):
            print(f"           judge reply unusable: {text[:120]!r}", file=sys.stderr)
    return {}


def check(board: dict, lines: list[str], i: int, images: Images, frame: Path) -> dict:
    metrics, failed = pixel_checks(frame)
    verdict = judge(board, lines, i, images, frame)
    failed += [c for c in CHECKS if verdict.get(c) is False] if verdict else ["not_judged"]
    problems = verdict.get("problems", "")
    if "colour_sparing" in failed:
        problems += " Too much colour; use colour only in lines and small accents."
    if "white_background" in failed:
        problems += " The background must be plain white, with no shading or fills."
    return {"passed": not failed, "failed": failed, "problems": problems.strip(), "metrics": metrics,
            "judged": bool(verdict)}


def draw_checked(outdir: Path, board: dict, lines: list[str], i: int, prompt: str, images: Images,
                 before_save=None) -> Path:
    """Draw frame i, check it, and redraw until it passes or MAX_ATTEMPTS is reached.
    before_save() runs just before the chosen attempt becomes frame-NN (edit.py backs up the old one)."""
    stem, hist = f"frame-{i:02d}", outdir / "history" / "qa"
    hist.mkdir(parents=True, exist_ok=True)
    attempts: list[tuple[Path, dict]] = []
    for k in range(1, MAX_ATTEMPTS + 1):
        last = attempts[-1] if attempts else None
        if last and set(last[1]["failed"]) <= LOCAL:  # only stray writing: fix it in place
            how = "edit"
            fix = load_prompt("edit_image", frame=i, n_frames=len(board["frames"]),
                              instruction=f"Fix this: {last[1]['problems']}",
                              words=frame_text(board, lines, i), scene=board["frames"][i - 1]["scene"])
            path = generate_image(fix, [("CURRENT FRAME (the one to edit)", last[0]), *images], hist, f"{stem}.try{k}")
        else:
            how = "draw" if not last else "redraw"
            feedback = (f"\n\nA PREVIOUS ATTEMPT AT THIS FRAME WAS REJECTED: {last[1]['problems']} "
                        "Draw it fresh and get this right.") if last else ""
            path = generate_image(prompt + feedback, images, hist, f"{stem}.try{k}")
        result = check(board, lines, i, images, path)
        attempts.append((path, result))
        status = "pass" if result["passed"] else f"FAIL {', '.join(result['failed'])}: {result['problems']}"
        print(f"           try {k} ({how}): {status}", flush=True)
        if result["passed"] or set(result["failed"]) <= NOT_REDRAWN:
            break
    # The latest attempt with the fewest failures.
    best_k = min(range(len(attempts)), key=lambda k: (len(attempts[k][1]["failed"]), -k))
    best, result = attempts[best_k]
    if before_save:
        before_save()
    final = outdir / f"{stem}{best.suffix}"
    shutil.copy(best, final)
    record(outdir, i, {**result, "attempts": len(attempts), "chosen": best.name})
    return final


def record(outdir: Path, i: int, entry: dict) -> None:
    path = outdir / "qa.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    data[str(i)] = entry
    path.write_text(json.dumps(dict(sorted(data.items(), key=lambda kv: int(kv[0]))), indent=2))


def summary(outdir: Path, board: dict) -> None:
    path = outdir / "qa.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    redrawn = [i for i, e in data.items() if e.get("attempts", 1) > 1]
    failed = {i: e for i, e in data.items() if not e.get("passed")}
    print(f"qa: {len(data)} frames checked, {len(redrawn)} redrawn, {len(failed)} still failing")
    for i, e in failed.items():
        hint = "re-check with `just qa`" if set(e["failed"]) <= NOT_REDRAWN else "fix with `just frame`"
        print(f"  frame {int(i):02d}: {', '.join(e['failed'])}: {e['problems']}  -> {hint}")


def report(outdir: Path, board: dict, lines: list[str]) -> None:
    """Check every existing frame and print the results (`just qa`); draws nothing."""
    for i in range(1, len(board["frames"]) + 1):
        frame = find_frame(outdir, i)
        if not frame:
            continue
        result = check(board, lines, i, frame_images(outdir, board, i), frame)
        m = result["metrics"]
        print(f"frame-{i:02d}: {'pass' if result['passed'] else 'FAIL ' + ', '.join(result['failed'])}"
              f"  [caption band ink {m['caption_band_ink']:.1%}, top ink {m['top_ink']:.1%},"
              f" colour {m['colour']:.1%}, white {m['white']:.0%}]"
              + (f"\n    {result['problems']}" if result["problems"] else ""), flush=True)
        record(outdir, i, {**result, "attempts": 0, "chosen": frame.name, "check_only": True})
