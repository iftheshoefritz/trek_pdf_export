#!/usr/bin/env python3
"""Bake a label-free Borg Layer_13.

Borg's Card_Background/Layer_13.png ships with the personnel attribute labels
INTEGRITY / CUNNING / STRENGTH baked into the bottom chrome strip. The
runtime renderer draws the row-appropriate labels as text on top, so the
baked labels cause:

  - personnel cards: text drawn over baked labels (close-but-not-exact alignment,
    reads as ghosted/double),
  - ship cards: correct Range/Weapons/Shields drawn over the wrong baked
    personnel labels — values are right, labels lie.

The PSD's intended fix (Ship/Attributes/No_Range.png etc. overlays) relies on
Photoshop layer effects and extracts as zero-alpha, same as the slot bases.
Rather than route this through Photopea too, we just paint out the bright
white text pixels inside the attribute band of Layer_13.png and ship a
sister asset Layer_13_no_labels.png that the Borg renderer points at.

Detection: pixels in the band where R,G,B > 150 are treated as glyph pixels.
Replacement: per-column median of the column's non-glyph pixels in the band.
"""
from pathlib import Path

from PIL import Image
import numpy as np

SRC = Path("extracted/borg/assets/Card_Background/Layer_13.png")
DST = Path("extracted/borg/assets/Card_Background/Layer_13_no_labels.png")

# Layer_13 pastes at canvas (28,26). Attribute bar is canvas y=921..946 →
# internal y=895..920.
PASTE_Y = 26
PASTE_X = 28
BAND_Y0 = 921 - PASTE_Y
BAND_Y1 = 946 - PASTE_Y

# The three baked label boxes inside the bar (canvas-space x ranges, detected
# by low-std-dev band scan against Layer_13). Limit cleanup to these so chrome
# highlights between the boxes aren't disturbed by a global threshold pass.
LABEL_BOXES_CANVAS = [(110, 261), (318, 473), (533, 685)]

# Glyph pixels are pure white (RGB 255,255,255) plus AA edges down to ~190.
# The box plate background is bluish-gray (~105,118,134) and must NOT trip
# the threshold or there are no "clean" rows left to sample a median from
# (the original 105 threshold over-caught the plate, leaving the loop
# unable to clean ~75% of columns). 200 catches glyphs + AA without
# touching plate pixels.
BRIGHT_THRESHOLD = 200


def main():
    src = Image.open(SRC).convert("RGBA")
    arr = np.array(src)
    band = arr[BAND_Y0:BAND_Y1, :, :3].astype(np.int16)
    bright = ((band[:, :, 0] > BRIGHT_THRESHOLD)
              & (band[:, :, 1] > BRIGHT_THRESHOLD)
              & (band[:, :, 2] > BRIGHT_THRESHOLD))

    out_band = arr[BAND_Y0:BAND_Y1, :, :].copy()
    cleared = 0
    for cx0, cx1 in LABEL_BOXES_CANVAS:
        x0 = cx0 - PASTE_X
        x1 = cx1 - PASTE_X
        # Per-column median replacement — the box has a subtle vertical
        # gradient (top of plate brighter than bottom), so a single median
        # would visibly darken the middle third where the text was densest.
        # Compute the median of each column's non-glyph rows independently.
        for x in range(x0, x1):
            col_mask = bright[:, x]
            if not col_mask.any():
                continue
            clean = band[~col_mask, x, :]
            if clean.shape[0] == 0:
                continue
            med = np.median(clean, axis=0).astype(np.uint8)
            out_band[col_mask, x, :3] = med
            cleared += int(col_mask.sum())
    arr[BAND_Y0:BAND_Y1, :, :] = out_band
    Image.fromarray(arr, "RGBA").save(DST)
    print(f"wrote {DST}; cleared {cleared} glyph pixels")


if __name__ == "__main__":
    main()
