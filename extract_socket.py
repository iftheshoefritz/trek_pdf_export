#!/usr/bin/env python3
"""
Extract the icon-socket chrome strip from the Federation Card_Background layer.

Slot 2 (the middle staffing icon) has its chrome socket — the ring frame plus
the small "tab" connecting it to the main text panel — permanently baked into
Card_Background/Layer_11.png. Slots 1 and 3 have no such socket in the PSD,
so reconstruct_card.py replicates this strip under those slots.

The strip is cropped tightly so it ends at the right chrome rim, BEFORE the
regular chrome-panel pattern begins. This matters because Layer_11's chrome
panel luminance differs across rows (slot 3 row ≈225, slot 1/2 rows ≈190);
copying slot 2's chrome panel pixels onto slot 3 would create a visible step.
A 5-column alpha taper on the right edge hides any residual seam by letting
the existing Layer_11 chrome panel show through.

Output: extracted/federation/assets/Card_Background/Icon_Socket.png (65×60)
  Socket centre is at (30, 30) within the asset — consumers (reconstruct_card)
  paste so that point lands on the slot's ring centre.

Re-run this script if Layer_11 is re-extracted or the asset is deleted.
"""

from pathlib import Path

import numpy as np
from PIL import Image

SRC = Path("extracted/federation/assets/Card_Background/Layer_11.png")
OUT = Path("extracted/federation/assets/Card_Background/Icon_Socket.png")

# Socket bbox in Layer_11 coords. Slot 2 ring centre is at (45, 694).
# x: 15..80   — left includes cyan-border edge; right ends at chrome rim
# y: 664..724 — vertical extent of the socket+tab
CROP = (15, 664, 80, 724)
TAPER_W = 5   # alpha-fade the rightmost N columns to blend into Layer_11 chrome


def main() -> None:
    src = Image.open(SRC).convert("RGBA")
    strip = src.crop(CROP)

    arr = np.array(strip)
    alpha = arr[:, :, 3].astype(float)
    width = arr.shape[1]
    for i in range(TAPER_W):
        col = width - TAPER_W + i
        alpha[:, col] *= 1.0 - (i + 1) / (TAPER_W + 1)
    arr[:, :, 3] = alpha.astype(np.uint8)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(arr).save(OUT)
    print(f"Saved {OUT}  ({arr.shape[1]}×{arr.shape[0]}) — socket centre = (30, 30)")


if __name__ == "__main__":
    main()
