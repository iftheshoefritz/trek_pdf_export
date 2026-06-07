#!/usr/bin/env python3
"""
Extract the assets reconstruct_card.py needs to render Dilemma cards.

Two sources, mirroring the two reasons psd-tools can't just composite the
templates as-is:

1. DILEMMA TEMPLATE TYPE ICONS  (templates/2e HD Dilemma v1.psd)
   The Type group holds three mutually-exclusive icons — Space, Planet and the
   combined Space/Planet ("dual") — only one of which is visible at a time.
   The layout extractor composites layers and so writes empty PNGs for the two
   hidden ones. We read their pixels directly with topil() (ignores visibility)
   and write all three to extracted/dilemma/assets/Type/.

2. AFFILIATION INLINE ICONS  (templates/2e HD Icon Set v1.psd)
   Dilemma game text references affiliations as inline icons, e.g. "[Car]" and
   "[Rom]". Those weren't in the inline set yet. They live as cells in the icon
   sprite sheet (same source as the staffing icons, see extract_icon_assets.py),
   downscaled to the 24x24 the inline renderer expects.
"""

from pathlib import Path

from PIL import Image
from psd_tools import PSDImage

DILEMMA_PSD  = Path("templates/2e HD Dilemma v1.psd")
ICON_SET_PSD = Path("templates/2e HD Icon Set v1.psd")
TYPE_DIR     = Path("extracted/dilemma/assets/Type")
INLINE_DIR   = Path("extracted/icons/inline")


# ---------------------------------------------------------------------------
# 1. Dilemma-type icons (Space / Planet / Dual)
# ---------------------------------------------------------------------------
# Layer name in the PSD -> output filename. "Space/Planet" is the dual icon
# (the only one left visible in the shipped template).
TYPE_LAYERS = {
    "Space":        "Space.png",
    "Planet":       "Planet.png",
    "Space/Planet": "Space_Planet.png",
}


def extract_type_icons():
    psd = PSDImage.open(DILEMMA_PSD)
    by_name = {l.name: l for l in psd.descendants()}
    TYPE_DIR.mkdir(parents=True, exist_ok=True)
    for layer_name, out_name in TYPE_LAYERS.items():
        layer = by_name[layer_name]
        im = layer.topil()   # raw layer pixels, regardless of visibility
        if im is None:
            print(f"  ! {layer_name}: no pixels")
            continue
        im = im.convert("RGBA")
        out = TYPE_DIR / out_name
        im.save(out)
        print(f"  {layer_name}: {im.size} -> {out}")


# ---------------------------------------------------------------------------
# 2. Affiliation inline icons from the icon sprite sheet
# ---------------------------------------------------------------------------
# Sprite geometry (see extract_icon_assets.py): 5 rows x 10 cols, cell bounds
# found by scanning the fully-transparent gutters between cells.
COL_BLOCKS = [
    (0, 30), (35, 66), (71, 101), (108, 137), (143, 174),
    (180, 210), (216, 247), (254, 285), (292, 321), (327, 356),
]
ROW_BLOCKS = [(0, 29), (37, 67), (74, 104), (112, 142), (149, 178)]

# Inline-icon stem -> sprite (row, col), identified by matching the cells
# against the named affiliation icons. Federation/Ferengi/Non-aligned already
# exist in the inline set; the rest are the affiliations dilemma text can cite.
AFFILIATION_CELLS = {
    "romulan":    (0, 1),
    "klingon":    (0, 2),
    "cardassian": (0, 6),
    "dominion":   (0, 7),
    "bajoran":    (0, 9),
}
INLINE_SIZE = 24   # matches the existing extracted/icons/inline/*.png


def extract_inline_affiliations():
    psd = PSDImage.open(ICON_SET_PSD)
    sprite = next(l for l in psd if l.name == "Icons").composite().convert("RGBA")
    INLINE_DIR.mkdir(parents=True, exist_ok=True)
    for stem, (r, c) in AFFILIATION_CELLS.items():
        ry1, ry2 = ROW_BLOCKS[r]
        cx1, cx2 = COL_BLOCKS[c]
        cell = sprite.crop((cx1, ry1, cx2 + 1, ry2 + 1)).resize(
            (INLINE_SIZE, INLINE_SIZE), Image.LANCZOS)
        out = INLINE_DIR / f"{stem}.png"
        cell.save(out)
        print(f"  {stem}: sprite r{r}c{c} -> {out} ({INLINE_SIZE}x{INLINE_SIZE})")


def main():
    print("Dilemma-type icons (from dilemma template):")
    extract_type_icons()
    print("Affiliation inline icons (from icon set sprite):")
    extract_inline_affiliations()


if __name__ == "__main__":
    main()
