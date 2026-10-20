"""Build frames: a frame that is the previous frame plus one new element, like the next bullet point
appearing on a slide.

The image model draws the addition as an edit of the previous frame. Even when told to change
nothing else it redraws the whole picture, so lines shift and faces change a little, and over a few
steps that adds up. lock() undoes it: it finds where the new drawing really differs from the
previous frame (the addition) and keeps the new pixels only there, copying the previous frame's
pixels everywhere else. So everything already on screen stays exactly as it was.

How the addition is found: ink (anything clearly not white) in the new drawing with no ink nearby
in the previous frame, or the reverse. "Nearby" absorbs the few pixels existing lines wobble by
when redrawn; a new object has no old ink near it. The found strokes are grown by PAD so the whole
object comes along with its edges, and the mask is feathered so there's no seam.

measure() gives what qa.py checks: how much of the frame changed (almost nothing means nothing was
added) and how much of the previous frame's ink changed (a lot means the model redrew the frame
instead of adding to it; a real addition only touches what it overlaps).
"""

from pathlib import Path

SCALE = 2            # work at 1/SCALE resolution: faster, and smooths anti-aliasing
INK = 0.3            # a pixel is ink if some channel is this far below white (0-1); catches colour too
WOBBLE = 0.008       # existing ink that moved less than this fraction of the width is the same ink (~6px)
PAD = 0.03           # grow the found strokes by this fraction of the width (~25px)
CHANGED_MIN = 0.002  # less of the frame than this changed: nothing was added (qa.py)
MOVED_MAX = 0.35     # more of the previous frame's ink than this changed: it was redrawn (qa.py)


def _odd(n: float) -> int:
    n = max(1, round(n))
    return n if n % 2 else n + 1


def _changes(prev: Path, new: Path):
    """(previous image, new image at the same size, change mask at 1/SCALE, {"changed", "moved"})."""
    import numpy as np
    from PIL import Image, ImageFilter

    a = Image.open(prev).convert("RGB")
    b = Image.open(new).convert("RGB")
    if b.size != a.size:
        b = b.resize(a.size, Image.LANCZOS)
    small = (max(1, a.width // SCALE), max(1, a.height // SCALE))

    def ink(im):
        px = np.asarray(im.resize(small, Image.BOX), dtype=np.float32) / 255
        return Image.fromarray(((1 - px.min(axis=2)) > INK).astype(np.uint8) * 255)

    grow = lambda m, frac: m.filter(ImageFilter.MaxFilter(_odd(2 * frac * small[0] + 1)))
    ia, ib = ink(a), ink(b)
    near_a, near_b = np.asarray(grow(ia, WOBBLE)) > 0, np.asarray(grow(ib, WOBBLE)) > 0
    diff = (np.asarray(ib) > 0) & ~near_a | (np.asarray(ia) > 0) & ~near_b
    mask = grow(Image.fromarray(diff.astype(np.uint8) * 255), PAD)
    m, old = np.asarray(mask) > 0, np.asarray(ia) > 0
    return a, b, mask, {"changed": float(m.mean()), "moved": float((m & old).sum() / max(1, old.sum()))}


def measure(prev: Path, new: Path) -> dict:
    """{"changed": fraction of the frame where new differs from prev (grown by PAD),
    "moved": fraction of prev's ink inside that area}."""
    return _changes(prev, new)[3]


def lock(prev: Path, new: Path, out: Path) -> Path:
    """Save to out: new where it differs from prev (the addition), prev everywhere else."""
    from PIL import Image, ImageFilter

    a, b, mask, _ = _changes(prev, new)
    feather = max(1, round(PAD * mask.width / 3))
    mask = mask.filter(ImageFilter.GaussianBlur(feather)).resize(a.size, Image.BILINEAR)
    Image.composite(b, a, mask).save(out)
    return out
