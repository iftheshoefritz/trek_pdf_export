#!/usr/bin/env python3
"""
Extract Personnel staffing icon assets for the Federation card template.

Produces 47×47 RGBA PNGs for the two icon slots whose layers have no usable
pixel data in the Federation PSD:
  - Slot 1: Staff      → extracted/federation/assets/.../Slot_1/Staff.png  (47×47)
  - Slot 3: Future     → extracted/federation/assets/.../Slot_3/Future.png (47×47)

The icons are sourced from the standalone icon sprite sheet (2e HD Icon Set v1.psd),
which contains complete pre-rendered ring+symbol composites on a transparent
background. Each sprite cell is ~30×30 native and is upscaled to 47×47 to match
the size of TNG.png (extracted from the Federation Ship group).

WHY THE SPRITE IS THE SOURCE
  Inactive Personnel staffing slot layers in the Federation PSD have empty
  underlying pixel data — their appearance in Photoshop comes entirely from
  layer effects (Bevel/Emboss) that psd-tools cannot render. The sprite sheet
  is the only source with usable pixels for these icons.

WHY SLOT 2 (TNG/DS9/VOYAGER/ETC.) NEEDS NO TREATMENT HERE
  The Slot 2 ring is permanently baked into Layer_11 of the Card_Background group,
  and the era-badge icon layers were extracted from the Ship group's active
  composite, so they already include the ring in their pixel data.
"""

from pathlib import Path

from PIL import Image
from psd_tools import PSDImage

ICON_SET_PSD = Path("templates/2e HD Icon Set v1.psd")
ASSETS       = Path("extracted/federation/assets/Staffing_and_Attributes/Personnel/Staffing")

RING_DIAMETER = 47   # match TNG.png's 47px dark-ring diameter


# ---------------------------------------------------------------------------
# Sprite geometry
# ---------------------------------------------------------------------------

# The "Icons" layer in the icon set PSD is a 357×179 sprite sheet at 300 DPI
# containing 5 rows × 10 columns of icons. Boundaries were found by scanning
# for fully-transparent columns/rows that separate cells.
COL_BLOCKS = [
    (0,30),(35,66),(71,101),(108,137),(143,174),
    (180,210),(216,247),(254,285),(292,321),(327,356),
]
ROW_BLOCKS = [(0,29),(37,67),(74,104),(112,142),(149,178)]

# Icon locations in the sprite (1-indexed, as identified by visual inspection
# of the exported grid). Extend this dict to add other icons as needed.
SPRITE_ICONS = {
    'Staff':  (4, 1),   # (row0, col0) 0-indexed  ← row 5 col 2 in 1-indexed
    'Future': (2, 2),   #                          ← row 3 col 3 in 1-indexed
}

# Maps icon name → output path
ICON_OUTPUTS = {
    'Staff':  ASSETS / 'Slot_1/Staff.png',
    'Future': ASSETS / 'Slot_3/Future.png',
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def crop_sprite(sprite: Image.Image, row0: int, col0: int) -> Image.Image:
    ry1, ry2 = ROW_BLOCKS[row0]
    cx1, cx2 = COL_BLOCKS[col0]
    return sprite.crop((cx1, ry1, cx2 + 1, ry2 + 1))


def dark_ring_bbox(img: Image.Image) -> tuple[int, int, int, int]:
    """Find the bbox of the dark ring (the visually defining circle border).

    The sprite cells have a ~3px transparent/anti-aliased halo around the actual
    ring. Scaling the full cell to 47×47 leaves the ring undersized compared to
    TNG. Measuring the ring directly lets us match TNG's apparent diameter.
    """
    import numpy as np
    a = np.asarray(img)
    rgb, alpha = a[:, :, :3], a[:, :, 3]
    lum = rgb.mean(axis=2)
    dark = (lum < 80) & (alpha > 128)
    rows = np.where(dark.any(axis=1))[0]
    cols = np.where(dark.any(axis=0))[0]
    return int(cols.min()), int(rows.min()), int(cols.max()) + 1, int(rows.max()) + 1


def upscale_to(img: Image.Image, ring_diameter: int) -> tuple[Image.Image, int, int]:
    """Scale img so its dark ring matches ring_diameter px, on a canvas large
    enough to contain the full scaled sprite (so the anti-aliased halo around
    the ring is not clipped).

    Returns (image, ring_cx, ring_cy) — the ring's centre coordinates within
    the returned image, so the caller can paste it with the ring landing at
    the right spot on the card.
    """
    rl, rt, rr, rb = dark_ring_bbox(img)
    ring_w, ring_h = rr - rl, rb - rt
    scale = ring_diameter / max(ring_w, ring_h)
    new_w = int(round(img.width * scale))
    new_h = int(round(img.height * scale))
    scaled = img.resize((new_w, new_h), Image.LANCZOS)
    ring_cx = int(round((rl + rr) / 2 * scale))
    ring_cy = int(round((rt + rb) / 2 * scale))
    return scaled, ring_cx, ring_cy


# ---------------------------------------------------------------------------
# Extract sprite cells and write upscaled icons
# ---------------------------------------------------------------------------

print("Loading icon set sprite…")
icon_psd = PSDImage.open(ICON_SET_PSD)
sprite_layer = next(l for l in icon_psd if l.name == "Icons")
sprite = sprite_layer.composite().convert("RGBA")
print(f"  Sprite size: {sprite.size}")

for name, (row0, col0) in SPRITE_ICONS.items():
    raw = crop_sprite(sprite, row0, col0)
    icon, ring_cx, ring_cy = upscale_to(raw, RING_DIAMETER)
    out_path = ICON_OUTPUTS[name]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    icon.save(out_path)
    # Sidecar metadata: the ring's centre within the PNG (consumers paste so
    # that this point lands at the slot's intended ring centre on the card).
    (out_path.with_suffix(".json")).write_text(
        f'{{"ring_cx": {ring_cx}, "ring_cy": {ring_cy}, '
        f'"ring_diameter": {RING_DIAMETER}}}\n'
    )
    print(f"  {name}: {raw.size} → {icon.size}  ring centre=({ring_cx},{ring_cy})")

print("\nDone.")
