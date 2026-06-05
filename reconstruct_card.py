#!/usr/bin/env python3
"""
Reconstruct Federation Personnel and Ship cards from the extracted PSD template
assets and the original low-quality card images as the character art. Card type
is read from each row's Type column; ships use a vertical staffing column (Staff
field), the Class oval, and Range/Weapons/Shields attributes, while personnel use
the skills row, Species oval, and Integrity/Cunning/Strength.

Usage:
    python3 reconstruct_card.py [INPUT.txt] [--dpi 300|600|800]

INPUT.txt is a tab-separated card data file with a header row (same column
layout as cards_with_processed_columns.txt / federation_personnel_fixture.txt).
Every row is rendered to OUTDIR/<CollectorsInfo>.png (300 DPI), or
OUTDIR/<dpi>dpi/<CollectorsInfo>.png for higher DPI. Layout is authored in
300-DPI design space and scaled by SCALE = dpi/300; text renders natively crisp,
raster template assets are LANCZOS-upscaled.

NAME and TITLE are not stored split in the data (column 1 concatenates them,
e.g. "Data Lucasian Chair"). They are inferred per card by OCR'ing the top
(name) row of the low-res card scan and fuzzy-aligning it against the known
tokens to find the split point. Low-confidence splits are flagged in the output.
A sidecar TSV (NAME_TITLE_OVERRIDE) can pin NAME/TITLE for any card OCR gets
wrong; it is empty by default. Requires the `tesseract` CLI on PATH.
"""

import csv
import json
import re
import shutil
import subprocess
import sys
from difflib import SequenceMatcher
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps

# All layout constants below are authored in the 300-DPI "design space"
# (a 736x1029 card). Output at other DPIs scales every measurement, font size
# and asset by SCALE = output_dpi / BASE_DPI. Text is drawn natively at the
# scaled size (stays crisp); raster assets are LANCZOS-upscaled (source-limited).
BASE_W, BASE_H = 736, 1029
BASE_DPI = 300
SUPPORTED_DPI = (300, 600, 800)
SCALE = 1.0   # set in main() from the chosen --dpi

def S(v):
    """Scale a design-space (300-DPI) measurement to the current output space."""
    return int(round(v * SCALE))

ASSETS = Path("extracted/federation/assets")
NONALIGNED_ASSETS = Path("extracted/nonaligned/assets")
KLINGON_ASSETS = Path("extracted/klingon/assets")
ROMULAN_ASSETS = Path("extracted/romulan/assets")
BORG_ASSETS = Path("extracted/borg/assets")
BAJORAN_ASSETS = Path("extracted/bajoran/assets")
DILEMMA_ASSETS = Path("extracted/dilemma/assets")
EVENT_ASSETS = Path("extracted/event/assets")
INTERRUPT_ASSETS = Path("extracted/interrupt/assets")
EQUIPMENT_ASSETS = Path("extracted/equipment/assets")
MISSION_ASSETS = Path("extracted/mission/assets")
INLINE_ICONS = Path("extracted/icons/inline")
FONTS  = Path("fonts")
PHOTOS = Path("fixture/low quality decipher images")
# Dilemma card art comes from the full-card scan library (keyed by ImageFile),
# searched after PHOTOS. Path is relative to the repo root (the script's cwd).
CARDIMAGES = Path("../webula/public/cardimages")
NAME_TITLE_OVERRIDE = Path("fixture/name_title_map.tsv")
# Per-card game text with <b>/<i> markup (bold = dilemma requirements etc.).
# There is no markup in the card data, so bold is recovered from the original
# scans by detect_bold_gametext.py and reviewed by hand. When a card appears
# here its marked-up text replaces the plain gametext at render time.
GAMETEXT_MARKUP = Path("fixture/gametext_bold.tsv")
DEFAULT_INPUT  = Path("fixture/federation_personnel_fixture.txt")
OUTDIR = Path("fixture/reconstructed")

# NAME/TITLE split inference via OCR of the card's top (name) row.
NAME_ROW_BOX = (92, 26, 340, 46)   # name-row band in the ~357x499 Decipher scan
# Missions: location names ("Second Moon of Bajor VIII") run longer than
# personnel names, so widen the band to nearly the full chrome bar.
MISSION_NAME_ROW_BOX = (85, 22, 355, 44)
OCR_UPSCALE = 5                    # upscale the tiny crop before OCR
OCR_TMP = Path(".ocr_tmp")
# A split is "confident" if the best candidate's fuzzy score is high OR it beats
# the runner-up by a clear margin. (Short single-word names score low in
# absolute terms but win by a wide margin, so either signal suffices.)
OCR_MIN_SCORE = 0.85
OCR_MIN_MARGIN = 0.15


# ---------------------------------------------------------------------------
# Card data loader
# ---------------------------------------------------------------------------
COLUMNS = ["Name", "Set", "ImageFile", "Rarity", "Unique", "CollectorsInfo",
           "Type", "Cost", "Mission", "DilemmaType", "Span", "Points",
           "Quadrant", "Affiliation", "Icons", "Staff", "Keywords", "Class",
           "Species", "Skills", "Integrity", "Range", "Cunning", "Weapons",
           "Strength", "Shields", "gametext", "HoF"]

def load_rows(path: Path) -> list[dict]:
    """Parse a tab-separated, double-quoted card file, skipping the header row.
    Uses the csv module so fields with embedded quotes (e.g. The Caretaker's
    "Guests") and tabs are unescaped correctly."""
    rows = []
    with path.open(newline="") as f:
        for parts in csv.reader(f, delimiter="\t", quotechar='"'):
            if len(parts) < 6 or parts[5] in ("", "CollectorsInfo"):
                continue  # header or blank line
            rows.append(dict(zip(COLUMNS, parts)))
    return rows


def load_name_title_overrides(path: Path) -> dict:
    """CollectorsInfo -> (name, title) overrides from a TSV with a header row.
    '#'-prefixed and short lines are ignored. Empty by default."""
    mapping = {}
    if not path.exists():
        return mapping
    with path.open() as f:
        for line in f:
            if line.startswith("#"):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 2 or parts[0] in ("", "CollectorsInfo"):
                continue
            title = parts[2] if len(parts) >= 3 else ""   # title-less names (many ships)
            mapping[parts[0]] = (parts[1], title)
    return mapping


def load_gametext_markup(path: Path) -> dict:
    """CollectorsInfo -> game text with <b>/<i> markup, from a TSV with a header
    row. '#'-prefixed and short lines are ignored. Empty/absent by default."""
    mapping = {}
    if not path.exists():
        return mapping
    with path.open() as f:
        for line in f:
            if line.startswith("#"):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 2 or parts[0] in ("", "CollectorsInfo"):
                continue
            mapping[parts[0]] = parts[1]
    return mapping


def find_photo(row: dict) -> Path:
    for base in (PHOTOS, CARDIMAGES):
        for stem in (row["CollectorsInfo"], row["ImageFile"]):
            p = base / f"{stem}.jpg"
            if p.exists():
                return p
    raise SystemExit(f"No photo found for {row['CollectorsInfo']}")


def _ocr_name_row(photo: Path, name_row_box=None) -> str:
    """OCR the top (name) row band of a card scan."""
    OCR_TMP.mkdir(exist_ok=True)
    im = Image.open(photo).convert("L")
    l, t, r, b = name_row_box if name_row_box is not None else NAME_ROW_BOX
    crop = im.crop((l, t, r, b)).resize(((r - l) * OCR_UPSCALE, (b - t) * OCR_UPSCALE))
    out = OCR_TMP / "name_row.png"
    crop.save(out)
    res = subprocess.run(["tesseract", str(out), "stdout", "--psm", "7"],
                         capture_output=True, text=True)
    return res.stdout.strip()


def _norm_words(s: str) -> list:
    return re.sub(r"[^a-z0-9 ]", " ", s.lower()).split()


def infer_name_title(full_name: str, photo: Path, name_row_box=None):
    """Split the concatenated column-1 name into (name, title) by OCR'ing the
    card's name row and fuzzy-aligning it against the known tokens. The OCR text
    is usually garbled, but we only need the boundary, so we score each candidate
    split point and take the best. Returns (name, title, score, margin).

    `name_row_box` defaults to the personnel/ship top-name band; mission scans
    use a slightly wider box (their location names run longer)."""
    tokens = full_name.split()
    if len(tokens) <= 1:
        return full_name, "", 1.0, 1.0
    # Prefixes that are never a complete name on their own — always absorb the
    # following token (e.g. "I.K.S. Rotarran ...", "U.S.S. Enterprise ...").
    MIN_NAME_TOKENS = 2 if tokens[0] in ("I.K.S.", "U.S.S.") else 1
    if len(tokens) <= MIN_NAME_TOKENS:
        return full_name, "", 1.0, 1.0
    ocr = " ".join(_norm_words(_ocr_name_row(photo, name_row_box)))
    scored = sorted(
        ((SequenceMatcher(None, " ".join(_norm_words(" ".join(tokens[:k]))), ocr).ratio(), k)
         for k in range(MIN_NAME_TOKENS, len(tokens))),
        reverse=True)
    best_score, best_k = scored[0]
    margin = best_score - (scored[1][0] if len(scored) > 1 else 0.0)
    return " ".join(tokens[:best_k]), " ".join(tokens[best_k:]), best_score, margin


def format_rarity(collectors_info: str) -> str:
    """14R078 → '14 R 78'; 0P010 → '0 P 10' (strip leading zeros on suffix)."""
    m = re.match(r"^(\d+)([A-Za-z]+)(\d+)$", collectors_info)
    if not m:
        return collectors_info
    prefix, letter, suffix = m.groups()
    return f"{prefix} {letter} {int(suffix)}"


# Reconstitute multi-word skill names (e.g. "2 Astrometrics") by joining a
# leading number with the following word. Skills in this dataset are either
# a single capitalised word, or "<n> <Word>".
def reflow_skills(tokens):
    out, i = [], 0
    while i < len(tokens):
        if tokens[i].isdigit() and i + 1 < len(tokens):
            out.append(f"{tokens[i]} {tokens[i+1]}")
            i += 2
        else:
            out.append(tokens[i])
            i += 1
    return out

# Actual card fonts from fonts/
F_CRILEE        = FONTS / "Crillee Italic BT.ttf"
F_FUTURA_BOLD   = FONTS / "Futura LT Condensed Bold.ttf"
F_FUTURA_BOLDO  = FONTS / "Futura LT Condensed Bold Oblique.ttf"
F_FUTURA_MED    = FONTS / "Futura LT Condensed Medium.ttf"
F_FUTURI_BOLD   = FONTS / "FUTURCB.TTF"    # FuturiCondensedBoldSWFTE
F_SWISS911_UCM  = FONTS / "Swiss911 UCm BT.ttf"  # ultra-compressed heavy display


def load_font(path, size):
    try:
        return ImageFont.truetype(str(path), max(1, int(size)))
    except Exception:
        return ImageFont.load_default()


_font_cache = {}

def gfont(path, design_size):
    """Font at a design-space point size, scaled to the current output space."""
    key = (str(path), S(design_size))
    f = _font_cache.get(key)
    if f is None:
        f = load_font(path, S(design_size))
        _font_cache[key] = f
    return f


def scale_asset(im):
    """Upscale a design-space (300-DPI) raster asset to the output space."""
    if SCALE == 1.0:
        return im
    return im.resize((max(1, round(im.width * SCALE)), max(1, round(im.height * SCALE))),
                     Image.LANCZOS)


def paste_rgba(canvas, asset_path, x, y):
    """Composite a design-space asset at device-space (x, y)."""
    src = scale_asset(Image.open(asset_path).convert("RGBA"))
    canvas.alpha_composite(src, dest=(int(x), int(y)))


def vcenter_y(bbox_top, bbox_h, font, text):
    """Return y so text is vertically centred within the bbox."""
    fb = font.getbbox(text)
    return bbox_top + (bbox_h - (fb[3] - fb[1])) // 2 - fb[1]


def apply_difference(base, top, dest):
    """Composite `top` onto `base` at `dest` using PSD Difference blend
    (|base - top| per RGB channel, masked by the layer's alpha)."""
    import numpy as np
    bx, by = dest
    tw, th = top.size
    region = base.crop((bx, by, bx + tw, by + th)).convert("RGBA")
    base_arr = np.array(region, dtype=np.int16)
    top_arr  = np.array(top.convert("RGBA"), dtype=np.int16)
    a = top_arr[:, :, 3:4] / 255.0
    diff = np.abs(base_arr[:, :, :3] - top_arr[:, :, :3])
    blended = (base_arr[:, :, :3] * (1 - a) + diff * a).astype(np.uint8)
    out = np.dstack([blended, base_arr[:, :, 3].astype(np.uint8)])
    base.paste(Image.fromarray(out, "RGBA"), (bx, by))


# ---------------------------------------------------------------------------
# Staffing icons — data-driven from card Icons field e.g. "[Stf][TNG][Fut]"
# ---------------------------------------------------------------------------
# Per-affiliation asset configs. Federation and Non-Aligned share the same
# layout (slot positions, text bboxes, fonts), they just differ in the chrome
# colourway and the card-background layer name. Federation also ships a
# standalone Icon_Socket.png that's pasted under slots 1 & 3 (its slot 2 socket
# is baked into Layer_11); Non-Aligned bakes all three sockets into Layer_13.
AFFIL_CFG = {
    "Federation": {
        "assets": ASSETS,
        "cardbg_layer": "Card_Background/Layer_11.png",
        # Federation bakes slot 2's socket into Layer_11, and slots 1 & 3
        # reuse the same Icon_Socket (a crop of slot 2 from Layer_11). The
        # asset's connector tab points up only; mirror=True lightens it onto
        # its own flip to also draw a tab pointing down.
        # Slot 4 has its own Base layer in the PSD (separate Photopea bake)
        # so it lives in per_slot_sockets instead of reusing Icon_Socket.
        "socket_asset": ASSETS / "Card_Background/Icon_Socket.png",
        "socket_slots": ("slot1", "slot3"),
        "socket_mirror": True,
        "socket_centre": (30, 30),
        "per_slot_sockets": {
            "slot4": ("Card_Background/Slot_4_Base.png", (40, 825)),
        },
        # Federation glyphs carry ring_cx/ring_cy sidecar metadata that
        # already corrects for off-centre visual content, so no global offset
        # is needed here. Slot 4 measured from spec.json AU bbox: delta=(0,+1).
        "ring_centre_offsets": {"slot1": (0, 0), "slot2": (0, 0), "slot3": (0, 0), "slot4": (0, +1)},
    },
    "Klingon": {
        "assets": KLINGON_ASSETS,
        # Klingon's PSD has no shared chrome-strip layer (Fed's Layer_11 /
        # NA's Layer_13). Card_Border.png is the analogous frame: it covers
        # the full inner card area (681x977 at offset 28,26) and provides
        # the wood/copper trim that flanks the staffing column. Per-slot
        # Bases are then dropped on top of it.
        "cardbg_layer": "Card_Background/Card_Border.png",
        "cardbg_paste": (28, 26),
        # Image_Frame is the comb-tooth photo notch strip — identical alpha
        # channel to Romulan/NA Layer_4.  Apply via difference blend (not
        # paste) so the starfield shows through, with solid-zone cutoff at
        # col 15 (same as Romulan/NA — sparse zone starts at col 15).
        # alpha=0.3: Klingon photo zones are darker than the notch pixels
        # (~41 RGB), so difference blend brightens them too strongly at
        # full alpha. Scale down to match the scan's subtle appearance.
        "photo_notch": "Card_Background/Image_Frame.png",
        "photo_notch_cutoff": 15,
        "photo_notch_alpha": 0.3,
        "per_slot_sockets": {
            "slot1": ("Card_Background/Slot_1_Base.png", (44, 620)),
            "slot2": ("Card_Background/Slot_2_Base.png", (41, 688)),
            "slot3": ("Card_Background/Slot_3_Base.png", (44, 757)),
            # Slot 4 not in PSD; if AU lands in slot 4 we reuse slot 3 shifted
            # down 67px the way NA does (matched ring rhythm).
        },
        "socket_asset": None,
        # PSD disc centres from spec.json icon bboxes vs global SLOT_RING_CENTRE.
        # Slot 4 not in PSD; mirrors slot 3 (slot 4 = slot 3 shifted down 67 px).
        "ring_centre_offsets": {"slot1": (-1, 0), "slot2": (0, +2), "slot3": (+2, 0), "slot4": (+2, 0)},
    },
    "Romulan": {
        "assets": ROMULAN_ASSETS,
        # Romulan's chrome frame is Layer_12 (full inner card area at 27,26).
        # Layer_4 solid zone runs cols 0-14; sparse zone starts at col 15.
        "cardbg_layer": "Card_Background/Layer_12.png",
        "cardbg_paste": (27, 26),
        "photo_notch_cutoff": 15,
        "per_slot_sockets": {
            "slot1": ("Card_Background/Slot_1_Base.png", (43, 618)),
            "slot2": ("Card_Background/Slot_2_Base.png", (42, 689)),
            "slot3": ("Card_Background/Slot_3_Base.png", (43, 756)),
        },
        "socket_asset": None,
        # Romulan PSD places disc centres 1-2 px left and 1 px above the
        # Klingon/NA positions; corrected per spec.json icon bbox measurement.
        # Slot 4 not in PSD; mirrors slot 3.
        "ring_centre_offsets": {"slot1": (-2, -1), "slot2": (0, +2), "slot3": (0, 0), "slot4": (0, 0)},
    },
    "Non-Aligned": {
        "assets": NONALIGNED_ASSETS,
        "cardbg_layer": "Card_Background/Layer_13.png",
        "photo_notch_cutoff": 15,
        "photo_notch_alpha": 0.3,
        # NA's PSD ships each Slot N/Base layer as a self-contained chunk of
        # the cardbg chrome with the socket disc cut into it — gold trim
        # slice on the left, beige chrome slice on the right, disc + up/down
        # connector tabs in the middle, all aligned to a specific (x, y) on
        # the canvas. Pasted at that native position the chrome slices line
        # up seamlessly with Layer_13 underneath. They CAN'T just be pasted
        # at slot ring centres — the trim/chrome bleed would land in the
        # wrong place. So each slot has its own asset + paste position.
        # Slot 4 is a copy of Slot 3 shifted down 67px (NA's PSD only has
        # three personnel slots; AU goes in a 4th printed-only position).
        "per_slot_sockets": {
            "slot1": ("Card_Background/Slot_1_Base.png", (43, 619)),
            "slot2": ("Card_Background/Slot_2_Base.png", (42, 689)),
            "slot3": ("Card_Background/Slot_3_Base.png", (42, 757)),
            "slot4": ("Card_Background/Slot_4_Base.png", (42, 824)),
        },
        # Fallback for any code path that still expects a single asset.
        "socket_asset": None,
        # Identical disc positions to Klingon in the PSD.
        # Slot 4 not in PSD; mirrors slot 3.
        "ring_centre_offsets": {"slot1": (-1, 0), "slot2": (0, +2), "slot3": (+2, 0), "slot4": (+2, 0)},
    },
    "Borg": {
        # Borg PSD has two stacked chrome layers (Layer_12 and Layer_13) and a
        # photo notch (Layer_4). Sets 1-14 Borg data: personnel Icons is always
        # exactly [Cmd] or [Stf] (no era stacking, no slot 1 socket disc — the
        # PSD has no Slot 1 Base); ships have empty Icons and 4-5 [Stf] in
        # Staff. So no per-slot sockets, no socket_asset, no socket_slots.
        "assets": BORG_ASSETS,
        # Layer_13_no_labels is Layer_13 with the personnel attribute labels
        # (INTEGRITY/CUNNING/STRENGTH) baked into the chrome painted out, so
        # the renderer can draw the correct labels for the row's type on top.
        # See bake_borg_chrome.py for the bake. Layer_12 is intentionally NOT
        # stacked here — duplicating it produces a second visible attribute
        # strip below the real one.
        "cardbg_layer": "Card_Background/Layer_13_no_labels.png",
        "cardbg_paste": (28, 26),
        "photo_notch_cutoff": 15,
        "per_slot_sockets": {},
        "socket_asset": None,
        # Borg slot 1 sits ~5 px right and ~3 px below Federation's
        # (PSD bbox (48,622,110,689) vs Fed (42,618,110,689)).
        "ring_centre_offsets": {"slot1": (+5, +3)},
        # Borg cost disc centred at (156.5, 74.5) per the PSD's Cost text bbox
        # (147,63,166,86) — about (+5,+4) from Federation's (152,71).
        "cost_centre": (157, 75),
        # Bottom attribute strip — Borg's bar sits at y=921 (vs Fed 916), height
        # 24, with each column slightly shifted (label/value x's per PSD bboxes
        # in the Ship.Attributes group: Range x=124, value-rt=237; Weapons
        # x=336, value-rt=451; Shields x=551, value-rt=666).
        "attr_label_x": (124, 336, 551),
        "attr_value_x": (237, 451, 666),
        "attr_bar_yh": (921, 24),
        # Card-name bar (PSD Card_Name bbox 200,60,445,89) and title bar
        # (PSD Title bbox 215,98,557,120) sit ~4 px right and ~5 px below
        # Federation's; right-edge of name bar comes from the PSD Rarity x
        # extent (676) to keep names from running into the chrome.
        "name_bar": (200, 60, 676, 29),     # left, top, right, height
        "title_bar": (215, 98, 22),         # x, top, height
        # Collectors-info chrome bbox (PSD Rarity 620,988,676,1002).
        "rarity_bbox": (620, 988, 676, 1002),
        # Class/Race oval (PSD Class/Race 293,557..448,594).
        "class_oval_bbox": (293, 557, 448, 594),
        # Ship staffing column: Borg PSD slot 1 starts at (53,171), 56 px
        # spacing down to slot 5 at (53,395) — shifted right and down vs
        # Federation's (49,168)+55 spacing.
        "ship_staff_slot_xy": [(53, 171), (53, 227), (53, 283), (53, 339), (53, 395)],
    },
    "Bajoran": {
        "assets": BAJORAN_ASSETS,
        # Bajoran's chrome frame is Layer_12 (bbox 31,31,705,1000); photo notch
        # is Layer_4 with the same solid-cols-0-14 zone as Romulan/NA/Klingon.
        "cardbg_layer": "Card_Background/Layer_12.png",
        "cardbg_paste": (31, 31),
        "photo_notch_cutoff": 15,
        "per_slot_sockets": {
            "slot1": ("Card_Background/Slot_1_Base.png", (40, 618)),
            "slot2": ("Card_Background/Slot_2_Base.png", (40, 687)),
            "slot3": ("Card_Background/Slot_3_Base.png", (40, 755)),
            "slot4": ("Card_Background/Slot_4_Base.png", (40, 823)),
        },
        "socket_asset": None,
        # Ring centres calibrated to the PSD's glyph bboxes (not the chrome disc
        # bboxes) — the Bajoran PSD draws each Cmd/Staff/era glyph ~5 px left of
        # its disc centre. PSD glyph centres: slot1 (72,652), slot2 (72.5,717.5),
        # slot3 (72.5,786.5), slot4 (72,855) vs global SLOT_RING_CENTRE.
        "ring_centre_offsets": {"slot1": (-2, -1), "slot2": (0, -3), "slot3": (0, -4), "slot4": (0, -2)},
        # Bajoran's printed cards use a wider skill / game-text band than the
        # PSD's Skill Text bbox (646) and Game Text bbox (632) suggest — text
        # extends out to the inner chrome edge (~660) on actual scans.
        "skill_right": 660,
        "text_right": 648,
    },
}
FED_CFG = AFFIL_CFG["Federation"]


def affil_cfg(row: dict) -> dict:
    """Pick the per-affiliation asset config for a personnel/ship row.
    Defaults to Federation when the row's affiliation isn't supported yet."""
    return AFFIL_CFG.get(row.get("Affiliation", "").strip(), FED_CFG)


# Icon sockets for slots 1 & 3 — only drawn under slots that actually have an
# icon. (Slot 2's socket is baked into Layer_11.) Federation-only; Non-Aligned
# bakes its sockets into the card background.
SOCKET_ASSET = ASSETS / "Card_Background/Icon_Socket.png"
SOCKET_CENTRE = (30, 30)

ICON_MAP = {
    'Cmd':  ('slot1', "Personnel/Staffing/Slot_1/Command.png"),
    'Stf':  ('slot1', "Personnel/Staffing/Slot_1/Staff.png"),
    'TNG':  ('slot2', "Personnel/Staffing/Slot_2/TNG.png"),
    'DS9':  ('slot2', "Personnel/Staffing/Slot_2/DS9.png"),
    'Voy':  ('slot2', "Personnel/Staffing/Slot_2/Voyager.png"),
    'TOS':  ('slot2', "Personnel/Staffing/Slot_2/TOS.png"),
    'Maq':  ('slot2', "Personnel/Staffing/Slot_2/Maquis.png"),
    'E':    ('slot2', "Personnel/Staffing/Slot_2/Earth.png"),
    'Fut':  ('slot3', "Personnel/Staffing/Slot_3/Future.png"),
    'Past': ('slot3', "Personnel/Staffing/Slot_3/Past.png"),
    'Pa':   ('slot3', "Personnel/Staffing/Slot_3/Past.png"),   # alias used in data
    # AU defaults to slot 3 (e.g. Vina [AU] shows it in the third position on
    # the printed card). render_icons promotes AU to slot 4 only when slot 3
    # is already filled by another icon (Past / Future). Asset path still
    # references Slot_3/AU.png — that's where the baked glyph is saved.
    'AU':   ('slot3', "Personnel/Staffing/Slot_3/AU.png"),
}

# Each slot's ring CENTRE on the card canvas. Calibrated visually against
# Klingon, Federation, and Non-Aligned scans — the x values are 2px right
# of the original psd-tools-derived positions (74 vs 72 for slot 1, 73 vs 72
# for slot 2) which improves icon centering across all affiliations.
SLOT_RING_CENTRE = {
    'slot1': (74, 653),
    'slot2': (73, 720),
    'slot3': (72, 790),
    'slot4': (72, 857),   # 67px below slot 3; AU sits here
}


def render_icons(canvas, icons_str, cfg=FED_CFG):
    """Paste sockets (slots 1 & 3) and icons. Slot 2's socket is baked into the
    card background. Non-Aligned bakes ALL three sockets into Layer_13, so for
    affiliations with `socket_asset=None` only the icons are pasted."""
    # Resolve each abbrev to (slot, asset_rel). AU defaults to slot 3 but is
    # promoted to slot 4 if slot 3 is already claimed by Past/Future on the
    # same card (printed cards e.g. Vina [AU] keep AU in slot 3; Slar Gorn
    # [Cmd][Pa][AU] pushes AU down to slot 4).
    abbrevs = re.findall(r'\[([^\]]+)\]', icons_str)
    resolved = []
    slot3_claimed_by_non_AU = any(
        ICON_MAP.get(a, (None,))[0] == 'slot3' and a != 'AU'
        for a in abbrevs
    )
    for abbrev in abbrevs:
        entry = ICON_MAP.get(abbrev)
        if not entry:
            print(f"  ! unknown icon: [{abbrev}]")
            continue
        slot, rel = entry
        if abbrev == 'AU' and slot3_claimed_by_non_AU:
            slot = 'slot4'
        resolved.append((abbrev, slot, rel))
    filled_slots = {s for _, s, _ in resolved}

    # The socket plate's bright chrome connector sits on its top edge only;
    # slot 2's baked socket connects both up and down. Mirror the plate onto
    # itself (pixel-wise lighten) so slots 1 & 3 get a bright connector at top
    # and bottom to match the middle slot.
    # Two socket models, each handling a (potentially overlapping) subset of
    # slots. Per-slot Bases (NA-style: self-contained cardbg chunks pasted at
    # native positions) win over the shared socket_asset for the slots they
    # cover; the shared socket is pasted under the remaining socket_slots.
    per_slot = cfg.get("per_slot_sockets") or {}
    handled = set()
    for slot, (rel, (px, py)) in per_slot.items():
        if slot not in filled_slots:
            continue
        asset = scale_asset(Image.open(cfg["assets"] / rel).convert("RGBA"))
        canvas.alpha_composite(asset, dest=(S(px), S(py)))
        handled.add(slot)
    if cfg["socket_asset"] is not None:
        socket = scale_asset(Image.open(cfg["socket_asset"]).convert("RGBA"))
        if cfg.get("socket_mirror", True):
            socket = ImageChops.lighter(socket, ImageOps.flip(socket))
        sc_x, sc_y = cfg.get("socket_centre", SOCKET_CENTRE)
        for slot in cfg.get("socket_slots", ("slot1", "slot3")):
            if slot in filled_slots and slot not in handled:
                cx, cy = SLOT_RING_CENTRE[slot]
                canvas.alpha_composite(socket, dest=(S(cx) - S(sc_x), S(cy) - S(sc_y)))

    offsets = cfg.get("ring_centre_offsets", {})
    ring_centres = {
        slot: (SLOT_RING_CENTRE[slot][0] + offsets.get(slot, (0, 0))[0],
               SLOT_RING_CENTRE[slot][1] + offsets.get(slot, (0, 0))[1])
        for slot in SLOT_RING_CENTRE
    }
    for _abbrev, slot, rel in resolved:
        asset_path = cfg["assets"] / "Staffing_and_Attributes" / rel
        cx, cy = ring_centres[slot]
        meta_path = asset_path.with_suffix(".json")
        src = scale_asset(Image.open(asset_path).convert("RGBA"))
        if meta_path.exists():
            m = json.loads(meta_path.read_text())
            rcx, rcy = S(m["ring_cx"]), S(m["ring_cy"])
        else:
            rcx, rcy = src.width // 2, src.height // 2
        canvas.alpha_composite(src, dest=(S(cx) - rcx, S(cy) - rcy))


# ---------------------------------------------------------------------------
# Ship staffing column — data-driven from the card's Staff field, e.g.
# "[Cmd][Cmd][Cmd][Stf]". Each bracket is one staffing requirement, stacked
# top-to-bottom down the left edge (up to 5 slots). Personnel use a different
# (3-slot) staffing layout handled by render_icons().
# ---------------------------------------------------------------------------
SHIP_STAFF_REL = {
    'Cmd': "Ship/Staffing/Slot_1/Command.png",
    'Stf': "Ship/Staffing/Slot_2/Staff.png",
}
# Design-space top-left of each slot's 49x49 socket (from spec.json bboxes).
SHIP_STAFF_SLOT_XY = [(49, 168), (49, 223), (49, 279), (49, 334), (49, 389)]


def render_ship_staffing(canvas, staff_str, cfg=FED_CFG):
    """Paste the vertical staffing-requirement icons for a ship from its Staff
    field. Brackets map top-to-bottom onto slots 1..5.

    The icon includes its dark socket disc (authentic — the printed card has a
    dark button behind each star, see the source scans), which also occludes the
    low-res scan's own printed star beneath. A transparent-disc star would let
    that scan star ghost through, slightly offset, so the disc stays."""
    base = cfg["assets"] / "Staffing_and_Attributes"
    slot_xy = cfg.get("ship_staff_slot_xy", SHIP_STAFF_SLOT_XY)
    abbrevs = re.findall(r'\[([^\]]+)\]', staff_str)
    for idx, abbrev in enumerate(abbrevs):
        if idx >= len(slot_xy):
            print(f"  ! more than {len(slot_xy)} staffing icons; ignoring extras")
            break
        rel = SHIP_STAFF_REL.get(abbrev)
        if not rel:
            print(f"  ! unknown ship staffing icon: [{abbrev}]")
            continue
        x, y = slot_xy[idx]
        paste_rgba(canvas, base / rel, S(x), S(y))


# ---------------------------------------------------------------------------
# Card font point sizes (design space). Derived from PDF: PDF_pts × (300/72) =
# PIL px @ 300 DPI. Confirmed against Benjamin Sisko in fixture/2eed_hires.pdf.
# Loaded per render via gfont() so they scale with the output DPI.
# ---------------------------------------------------------------------------
PT_COST, PT_NAME, PT_TITLE, PT_SPECIES = 33, 35, 27, 33
PT_SKILL, PT_GAME, PT_ATTR, PT_RARITY = 29, 29, 28, 17
PT_ATTR_CAP, PT_ATTR_SC = 28, 21

# The black cost circle is in the same canvas position on every card type
# (Personnel/Ship/Event/Dilemma all paste their background at the same offset),
# so the cost number's center is universal. Measured from the rendered chrome:
# the dark disc spans canvas x≈139..165, y≈58..86 in 300-DPI design space.
COST_CIRCLE_CX, COST_CIRCLE_CY = 152, 71


def draw_cost(draw, cost_text, centre=(COST_CIRCLE_CX, COST_CIRCLE_CY)):
    """Draw the white cost number centered on the black cost circle.

    Centering is by the glyph's visible bbox (not advance width) so italic
    digits like '1' don't appear left-shifted. `centre` is a per-affiliation
    design-space (x, y) for the cost disc; Borg's chrome puts it ~5 px right
    and ~4 px lower than Federation's.
    """
    ccx, ccy = centre
    font = gfont(F_CRILEE, PT_COST)
    l, t, r, b = font.getbbox(cost_text)
    cx = S(ccx) - (l + r) // 2
    cy = S(ccy) - (t + b) // 2
    draw.text((cx, cy), cost_text, font=font, fill=(255, 255, 255, 255))


# ---------------------------------------------------------------------------
# Rich text flow — used for the lore/keyword line and the game text.
# Supports: bold/medium/italic runs, inline icons ([TNG] etc.), uniform line
# height (always room for an icon), and auto-shrink to fit the box without
# truncating (Decipher cards likewise shrink dense game text to fit).
# ---------------------------------------------------------------------------
STYLE_FONT = {'bold': F_FUTURA_BOLD, 'med': F_FUTURA_MED, 'italic': F_FUTURA_BOLDO}

INLINE_ICON_DIR = Path("extracted/icons/inline")
# Text icon abbreviation -> inline icon file stem (extracted/icons/inline/<stem>.png)
INLINE_ICON_MAP = {
    # Staffing / command
    'Cmd': 'command', 'Stf': 'staff',
    # Affiliations / series
    'TNG': 'tng', 'TN': 'tng', 'DS9': 'ds9', 'Voy': 'voyager', 'TOS': 'tos',
    'Maq': 'maquis', 'E': 'earth',
    'Fed': 'federation', 'NA': 'nonaligned', 'Non': 'nonaligned',
    'Fer': 'ferengi', 'Rom': 'romulan', 'Kli': 'klingon',
    'Car': 'cardassian', 'Dom': 'dominion', 'Baj': 'bajoran',
    'Bor': 'borg', 'Vid': 'vidiian',
    'SF': 'starfleet', 'Sta': 'starfleet',
    # Time-period swirls
    'Fut': 'future', 'AU': 'au', 'Past': 'past', 'Pa': 'past',
    # Quadrant letters
    'AQ': 'quadrant_alpha', 'GQ': 'quadrant_gamma', 'DQ': 'quadrant_delta',
    # Mission/type marks
    'P': 'planet', 'S': 'space', 'Dual': 'dual', 'D': 'dual', 'HQ': 'headquarters',
    'Equ': 'equipment', 'Ev': 'event', 'Int': 'interrupt',
}
LEADING_RATIO = 31.25 / 29   # uniform line height as a multiple of font size

_inline_icon_cache = {}

def inline_icon(abbrev, height):
    """Return the inline icon for a text abbreviation scaled to `height` px,
    or None if there is no asset for it."""
    stem = INLINE_ICON_MAP.get(abbrev)
    if not stem:
        return None
    path = INLINE_ICON_DIR / f"{stem}.png"
    if not path.exists():
        return None
    key = (stem, height)
    if key not in _inline_icon_cache:
        im = Image.open(path).convert("RGBA")
        w = max(1, round(im.width * height / im.height))
        _inline_icon_cache[key] = im.resize((w, height), Image.LANCZOS)
    return _inline_icon_cache[key]


def strip_braces(text):
    """Remove the curly brackets that wrap named-card references in card text
    (e.g. '{Bajor}', "{Caretaker's Array}"), keeping the name itself."""
    return text.replace("{", "").replace("}", "")


def parse_markup_runs(text, default='med'):
    """Split text carrying <b>..</b> / <i>..</i> tags into styled runs. Bold
    spans (the dilemma requirements etc.) map to 'bold', italics to 'italic',
    everything else to `default`. Tags may not nest in this data."""
    runs, stack, pos = [], [default], 0
    for m in re.finditer(r'</?[bi]>', text):
        seg = text[pos:m.start()]
        if seg:
            runs.append((seg, stack[-1]))
        tag = m.group(0)
        if tag[1] == '/':
            if len(stack) > 1:
                stack.pop()
        else:
            stack.append('bold' if tag[1] == 'b' else 'italic')
        pos = m.end()
    seg = text[pos:]
    if seg:
        runs.append((seg, stack[-1]))
    return runs


def gametext_runs(text):
    """Styled runs for game text. Explicit <b>/<i> markup (from the bold sidecar)
    wins; otherwise fall back to bolding a leading 'Order -'-style lexeme."""
    text = strip_braces(text.strip())
    if '<b>' in text or '<i>' in text:
        return parse_markup_runs(text, default='med')
    m = re.match(r'^([A-Z][A-Za-z]+ -)(.*)$', text)
    if m:
        return [(m.group(1), 'bold'), (m.group(2), 'med')]
    return [(text, 'med')]


def keyword_runs(text):
    """Styled runs for the keyword line. A keyword phrase is "<Type>" or
    "<Type>: <value>" and the line may chain several phrases separated by
    ". " — e.g. "Commander: Fortune. Thief." → Commander+Fortune in one
    bold/italic pair, then Thief in bold.

    A ". " is treated as a phrase boundary only when the period is NOT
    preceded by an uppercase letter — that exempts abbreviations and Roman
    numerals like "U.S.S. Defiant" or "Bajor VIII" that would otherwise
    fragment mid-value."""
    text = strip_braces(text.strip())
    # Split at ". " when the character before the period is lowercase/digit/punct
    # (not an A–Z letter, which would mean an abbreviation or Roman numeral).
    splits = re.split(r'(?<=[^A-Z])\. (?=\S)', text)
    phrases = [(p + '. ') for p in splits[:-1]] + [splits[-1]]

    runs = []
    for phrase in phrases:
        if ': ' in phrase:
            head, tail = phrase.split(': ', 1)
            runs.append((head + ': ', 'bold'))
            runs.append((tail, 'italic'))
        else:
            runs.append((phrase, 'bold'))
    return runs


def _flow_tokens(styled_runs, size):
    """Tokenise styled runs at a given font size into (kind, payload, width, font).

    An inline icon glued to the word that follows it (no space between, e.g.
    "[HQ]Bajor") is merged into one atomic 'iconword' token so the wrapper never
    splits the icon from its word across lines; the trailing space token still
    separates the unit from the next word."""
    space_w = load_font(F_FUTURA_MED, size).getlength(" ")
    icon_h = round(size * 1.18)   # extends a little above and below the text
    tokens = []
    for text, style in styled_runs:
        font = load_font(STYLE_FONT[style], size)
        for piece in re.split(r'(\[[^\]]+\]|\s+)', text):
            if not piece:
                continue
            if piece.isspace():
                tokens.append(('space', None, space_w, None))
            elif re.fullmatch(r'\[[^\]]+\]', piece):
                icon = inline_icon(piece[1:-1], icon_h)
                if icon is not None:
                    tokens.append(('icon', icon, icon.width, None))
                else:
                    tokens.append(('word', piece, font.getlength(piece), font))
            else:
                tokens.append(('word', piece, font.getlength(piece), font))

    icon_gap = max(1, round(space_w * 0.5))   # small gap between icon and its word
    merged = []
    i = 0
    while i < len(tokens):
        kind, payload, tw, font = tokens[i]
        if kind == 'icon' and i + 1 < len(tokens) and tokens[i + 1][0] == 'word':
            _, wtext, wtw, wfont = tokens[i + 1]
            merged.append(('iconword', (payload, icon_gap, wtext, wfont),
                           tw + icon_gap + wtw, None))
            i += 2
        else:
            merged.append(tokens[i])
            i += 1
    return merged


def _wrap_tokens(tokens, max_w):
    lines, cur, w = [], [], 0
    for tok in tokens:
        kind, _, tw, _ = tok
        if kind == 'space':
            if cur:
                cur.append(tok); w += tw
        else:
            if cur and w + tw > max_w:
                while cur and cur[-1][0] == 'space':
                    w -= cur[-1][2]; cur.pop()
                lines.append(cur)
                cur, w = [tok], tw
            else:
                cur.append(tok); w += tw
    if cur:
        while cur and cur[-1][0] == 'space':
            cur.pop()
        lines.append(cur)
    return lines


def draw_textflow(canvas, draw, styled_runs, box, fill, base_size, min_size=None):
    """Lay out styled_runs in design-space box=[l,t,r,b], shrinking the font from
    base_size down to min_size (design-space point sizes) until every wrapped line
    fits the box height. Box and sizes are scaled to the output space internally.
    Text is left-aligned (ragged right), matching the printed cards. Returns the
    y just below the last line."""
    if min_size is None:
        min_size = base_size
    left, top, right, bottom = S(box[0]), S(box[1]), S(box[2]), S(box[3])
    max_w = right - left
    base_size, min_size = S(base_size), S(min_size)

    size = base_size
    while True:
        lines = _wrap_tokens(_flow_tokens(styled_runs, size), max_w)
        if top + len(lines) * (size * LEADING_RATIO) <= bottom or size <= min_size:
            break
        size -= 1

    line_h = size * LEADING_RATIO
    ref = load_font(F_FUTURA_MED, size)
    hb = ref.getbbox("H")
    cap_mid_off = (hb[1] + hb[3]) / 2

    y = top
    for line in lines:
        x = left
        cap_mid = y + cap_mid_off
        for kind, payload, tw, font in line:
            if kind == 'word':
                draw.text((x, y), payload, font=font, fill=fill)
            elif kind == 'icon':
                canvas.alpha_composite(payload, dest=(int(round(x)), int(round(cap_mid - payload.height / 2))))
            elif kind == 'iconword':
                icon_img, gap, wtext, wfont = payload
                canvas.alpha_composite(icon_img, dest=(int(round(x)), int(round(cap_mid - icon_img.height / 2))))
                draw.text((int(round(x + icon_img.width + gap)), y), wtext, font=wfont, fill=fill)
            x += tw
        y += line_h
    return y


# ---------------------------------------------------------------------------
# Build canvas — spec.json order is bottom-to-top (index 0 = bottommost)
# ---------------------------------------------------------------------------

def render_card(ROW: dict, NAME: str, TITLE: str) -> Image.Image:
    is_ship = ROW["Type"].strip().lower() == "ship"
    cfg = affil_cfg(ROW)
    assets = cfg["assets"]

    canvas = Image.new("RGBA", (S(BASE_W), S(BASE_H)), (0, 0, 0, 0))

    # 1. Black Border — background, provides outer card colour
    paste_rgba(canvas, assets / "Black_Border.png", S(-2), S(-2))

    # 2. Character art: scale original low-res card to canvas, crop to photo window
    photo_src = Image.open(find_photo(ROW)).convert("RGBA")
    photo_full = photo_src.resize((S(BASE_W), S(BASE_H)), Image.LANCZOS)
    photo_window = photo_full.crop((S(32), S(140), S(672), S(574)))
    canvas.alpha_composite(photo_window, dest=(S(32), S(140)))


    # 3. Card Background.
    # Zero out partial-alpha chrome pixels inside the photo window before compositing.
    # The chrome assets bleed partial-alpha staffing-column decoration into the photo
    # area, which shows as a thin notch strip between the staffing column and the icons.
    # The photo window in canvas space is x=32-672, y=140-574.
    # Affiliations may have one chrome layer (cardbg_layer + cardbg_paste) or
    # several stacked (cardbg_layers: list of (rel, (x,y))). Borg stacks
    # Layer_12 over Layer_13; both need the same photo-window alpha-zero.
    if "cardbg_layers" in cfg:
        chrome_layers = list(cfg["cardbg_layers"])
    else:
        chrome_layers = [(cfg["cardbg_layer"], cfg.get("cardbg_paste", (27, 26)))]
    import numpy as _np
    for rel, (cbx, cby) in chrome_layers:
        chrome_img = scale_asset(Image.open(assets / rel).convert("RGBA"))
        _ca = _np.array(chrome_img)
        # Zero chrome's alpha inside the photo window so the photo shows through,
        # but stop SHORT of the class/race strip (y=552+) and the top-left
        # affiliation chrome (y<153 in the leftmost staffing column for ships).
        # The chrome's opaque class band and top-left curls should remain visible.
        _pw_x0 = S(32) - S(cbx)
        _pw_y0 = S(140) - S(cby)
        _pw_x1 = S(672) - S(cbx)
        _pw_y1 = S(552) - S(cby)
        _ca[max(0,_pw_y0):_pw_y1, max(0,_pw_x0):_pw_x1, 3] = 0
        if is_ship:
            # Re-zero the staffing strip x=32..49 from y=153 onwards (the curls at
            # y=140..152 stay intact); covers the band from y=552 down to where
            # the per-slot socket discs begin at y=620.
            _ssy0 = max(0, S(153) - S(cby))
            _ssy1 = S(620) - S(cby)
            _ssx0 = max(0, S(32) - S(cbx))
            _ssx1 = S(49) - S(cbx)
            _ca[_ssy0:_ssy1, _ssx0:_ssx1, 3] = 0
        chrome_img = Image.fromarray(_ca)
        canvas.alpha_composite(chrome_img, dest=(S(cbx), S(cby)))
    photo_notch = cfg.get("photo_notch", "Card_Background/Layer_4.png")
    if photo_notch:
        layer4 = scale_asset(Image.open(assets / photo_notch).convert("RGBA"))
        # Solid notch zones differ by affiliation (300-DPI col counts):
        #   Federation: cols 0-7 solid, sparse tips at 8-14 → cutoff 8
        #   Romulan / Non-Aligned / Klingon: cols 0-14 solid → cutoff 15
        # photo_notch_alpha scales the layer alpha before the blend so the
        # notch effect matches the scan's subtlety on the card's photo content.
        notch_cutoff = cfg.get("photo_notch_cutoff", 8)
        notch_alpha = cfg.get("photo_notch_alpha", 1.0)
        import numpy as _np2
        _l = _np2.array(layer4)
        _l[:, S(notch_cutoff):, 3] = 0
        if notch_alpha != 1.0:
            _l[:, :, 3] = (_l[:, :, 3] * notch_alpha).astype(_np2.uint8)
        layer4 = Image.fromarray(_l)
        apply_difference(canvas, layer4, (S(32), S(140)))

    # 4. Staffing / affiliation icons — data-driven from the card's fields.
    if is_ship:
        # Ships: vertical staffing-requirement column (Staff field) plus the
        # affiliation/era icon (Icons field) reusing the slot 2/3 sockets.
        render_ship_staffing(canvas, ROW["Staff"], cfg)
        render_icons(canvas, ROW["Icons"], cfg)
    else:
        # Personnel: staffing icons come entirely from the Icons field
        # e.g. "[Stf][TNG][Fut]"
        render_icons(canvas, ROW["Icons"], cfg)

    # 5. Attribute labels bar background (asset is transparent; labels rendered as text below)

    # -----------------------------------------------------------------------
    # Text overlays
    # Sizes calibrated via getbbox() measurements against spec.json layer bboxes.
    # -----------------------------------------------------------------------

    draw = ImageDraw.Draw(canvas)
    BLACK = (0, 0, 0, 255)
    WHITE = (255, 255, 255, 255)

    draw_cost(draw, ROW["Cost"], centre=cfg.get("cost_centre", (COST_CIRCLE_CX, COST_CIRCLE_CY)))

    # Name — black, wraps when long, unique dot when flagged.
    # Bar bbox per-affil from PSD's Card_Name layer (Fed: 196,56,439,86;
    # Borg: 200,60,445,89).
    name_bar = cfg.get("name_bar", (196, 56, 670, 30))  # (left, top, right, h)
    draw_card_name(canvas, draw, NAME, ROW["Unique"].upper() == "Y",
                   bar_top=name_bar[1], bar_h=name_bar[3],
                   base_x=name_bar[0], right_edge=name_bar[2], color=BLACK)

    # Title — vert-centred in the title bar background (Fed: y=93..115;
    # Borg: y=98..120).
    font_title = gfont(F_CRILEE, PT_TITLE)
    title_bar = cfg.get("title_bar", (210, 93, 22))  # (x, top, h)
    title_y = vcenter_y(S(title_bar[1]), S(title_bar[2]), font_title, TITLE)
    draw.text((S(title_bar[0]), title_y), TITLE, font=font_title, fill=BLACK)

    # Class/Race oval — centred in the per-affil bbox (Fed 287,552..440,588;
    # Borg 293,557..448,594). Ships show their Class (e.g. "Defiant Class");
    # personnel show their Species. Ship Class fields ending in literal
    # " Class" render the class name in italic and " Class" upright
    # (e.g. *Defiant* Class). Fields without that suffix
    # (e.g. "Flaxian Scout Vessel") render upright as a whole.
    cr_l, cr_t, cr_r, cr_b = cfg.get("class_oval_bbox", (287, 552, 440, 588))
    font_species = gfont(F_FUTURA_BOLD, PT_SPECIES)
    species_text = ROW["Class"] if is_ship else ROW["Species"]
    cx = (S(cr_l) + S(cr_r)) // 2
    sp_y = vcenter_y(S(cr_t), S(cr_b - cr_t), font_species, species_text)
    if is_ship and species_text.endswith(" Class"):
        italic_part = species_text[:-len(" Class")]
        upright_part = " Class"
        font_italic = gfont(F_FUTURA_BOLDO, PT_SPECIES)
        w_italic = int(draw.textlength(italic_part, font=font_italic))
        w_upright = int(draw.textlength(upright_part, font=font_species))
        total_w = w_italic + w_upright
        x = cx - total_w // 2
        draw.text((x, sp_y), italic_part, font=font_italic, fill=BLACK)
        draw.text((x + w_italic, sp_y), upright_part, font=font_species, fill=BLACK)
    else:
        sp_tw = int(draw.textlength(species_text, font=font_species))
        draw.text((cx - sp_tw // 2, sp_y), species_text, font=font_species, fill=BLACK)

    # Skills: flow layout — personnel only (ships have no skills line). Wrap at
    # the Skill Text right edge (spec: x=646), spacing from spec measurements.
    # dot_w=21px, dot-to-text gap=4px, inter-skill gap=12px, row spacing=33px
    cur_row = 0
    if not is_ship:
        SKILLS = reflow_skills(ROW["Skills"].split())
        font_skill = gfont(F_FUTURA_BOLD, PT_SKILL)
        skills = SKILLS
        dot_path = assets / "Skills_and_Flavor_Text/Personnel/Skill_1/Dot.png"
        DOT_W, DOT_TEXT_GAP, INTER_GAP = S(21), S(4), S(12)
        SKILL_LEFT, SKILL_RIGHT = S(126), S(cfg.get("skill_right", 646))
        ROW0_TEXT_Y, ROW_SPACING = S(644), S(33)

        DOT_H = S(21)
        # Anchor both the text origin and the dot at fixed offsets derived from font
        # metrics, so they don't shift per-word depending on whether the skill has
        # ascenders/descenders (e.g. "Officer" has none, "Engineer" has a g).
        # Reference cap measured against "H" (no descender, full cap height)
        H_TOP = font_skill.getbbox("H")[1]
        H_BOT = font_skill.getbbox("H")[3]
        CAP_H = H_BOT - H_TOP

        x = SKILL_LEFT
        for skill in skills:
            tw = int(draw.textlength(skill, font=font_skill))
            slot_w = DOT_W + DOT_TEXT_GAP + tw + INTER_GAP
            if x > SKILL_LEFT and x + slot_w - INTER_GAP > SKILL_RIGHT:
                cur_row += 1
                x = SKILL_LEFT
            text_y = ROW0_TEXT_Y + cur_row * ROW_SPACING
            # Place every skill so its cap top sits at the same y; same dot for the row.
            sk_y = text_y + (S(23) - CAP_H) // 2 - H_TOP
            cap_mid = sk_y + H_TOP + CAP_H / 2
            dot_y = int(cap_mid - DOT_H / 2)
            paste_rgba(canvas, dot_path, x, dot_y)
            draw.text((x + DOT_W + DOT_TEXT_GAP, sk_y), skill, font=font_skill, fill=BLACK)
            x += slot_w

    # Lore/keyword line + game text share the text band below the skills, down
    # to the bottom of the Skills and Flavor Text group (spec: y=840). The game
    # text auto-shrinks to fit rather than truncating. (Design-space coords;
    # draw_textflow scales them to the output space.)
    TEXT_LEFT, TEXT_RIGHT, TEXT_BOTTOM = 126, cfg.get("text_right", 632), 840
    if is_ship:
        # No skills row; start the text band at the top of the Skills/Flavor group.
        block_top = 648
    else:
        block_top = 644 + cur_row * 33 + 35   # design space; cur_row is a 0-based count

    # Keyword line + game text. The game text continues inline on the keyword's
    # line (one wrapped flow) when there is room — except an "Order -"-style game
    # text (a bold leading lexeme) starts on its own line, so keyword and game
    # text are then drawn as two separate blocks.
    keywords_text = ROW["Keywords"].strip()
    gametext = ROW["gametext"]
    game_runs = gametext_runs(gametext)
    is_order = bool(re.match(r'^[A-Z][A-Za-z]+ -', gametext.strip()))

    if keywords_text and not is_order:
        combined = keyword_runs(keywords_text) + [(" ", "med")] + game_runs
        draw_textflow(canvas, draw, combined,
                      [TEXT_LEFT, block_top, TEXT_RIGHT, TEXT_BOTTOM], BLACK, PT_GAME, min_size=15)
    else:
        if keywords_text:
            draw_textflow(canvas, draw, keyword_runs(keywords_text),
                          [TEXT_LEFT, block_top, TEXT_RIGHT, block_top + 40], BLACK, PT_GAME)
            game_top = block_top + int(round(PT_GAME * LEADING_RATIO))
        else:
            game_top = block_top
        draw_textflow(canvas, draw, game_runs,
                      [TEXT_LEFT, game_top, TEXT_RIGHT, TEXT_BOTTOM], BLACK, PT_GAME, min_size=15)

    # Attribute labels — small-caps: first letter at 28px, remainder uppercase at 21px.
    # Ships show Range/Weapons/Shields; personnel show Integrity/Cunning/Strength.
    # Bar geometry varies per affiliation (Borg's chrome puts the strip 5 px
    # lower and shifts each column slightly), so the row of label x's,
    # value-right-edge x's, and bar (top, height) are all read from cfg with
    # Federation defaults.
    label_x = cfg.get("attr_label_x", (126, 335, 548))
    value_x = cfg.get("attr_value_x", (237, 449, 662))
    bar_y, bar_h_ds = cfg.get("attr_bar_yh", (916, 23))
    if is_ship:
        attr_labels = list(zip(("Range", "Weapons", "Shields"), [S(x) for x in label_x]))
        attr_values = list(zip((ROW["Range"], ROW["Weapons"], ROW["Shields"]), [S(x) for x in value_x]))
    else:
        attr_labels = list(zip(("Integrity", "Cunning", "Strength"), [S(x) for x in label_x]))
        attr_values = list(zip((ROW["Integrity"], ROW["Cunning"], ROW["Strength"]), [S(x) for x in value_x]))

    font_attr_cap = gfont(F_FUTURI_BOLD, PT_ATTR_CAP)
    font_attr_sc = gfont(F_FUTURI_BOLD, PT_ATTR_SC)
    bar_top, bar_h = S(bar_y), S(bar_h_ds)
    # Anchor every label off a reference cap "H" (full-height, no descender)
    # so caps share a baseline across labels regardless of which letter starts
    # each word — using each glyph's own bbox here gives I/W/R/C/S different
    # absolute y's and lands the middle column visibly out of line.
    H_top_cap = font_attr_cap.getbbox("H")[1]
    H_bot_cap = font_attr_cap.getbbox("H")[3]
    H_h_cap = H_bot_cap - H_top_cap
    cap_y = bar_top + (bar_h - H_h_cap) // 2 - H_top_cap
    cap_baseline = cap_y + H_bot_cap
    H_bot_sc = font_attr_sc.getbbox("H")[3]
    rest_y = cap_baseline - H_bot_sc
    for label, lx in attr_labels:
        cap, rest = label[0], label[1:].upper()
        draw.text((lx, cap_y), cap, font=font_attr_cap, fill=WHITE)
        cap_w = int(draw.textlength(cap, font=font_attr_cap))
        draw.text((lx + cap_w, rest_y), rest, font=font_attr_sc, fill=WHITE)

    # Attribute values — right-aligned to the per-affil value_x right-edges,
    # vertically anchored to the same reference baseline so 1/8/12 don't drift.
    font_attr = gfont(F_FUTURI_BOLD, PT_ATTR)
    H_top_v = font_attr.getbbox("H")[1]
    H_bot_v = font_attr.getbbox("H")[3]
    vy = bar_top + (bar_h - (H_bot_v - H_top_v)) // 2 - H_top_v
    for val, rx in attr_values:
        tw = int(draw.textlength(val, font=font_attr))
        draw.text((rx - tw, vy), val, font=font_attr, fill=WHITE)

    # Rarity / collectors info — centred in the small chrome bbox per-affil
    # (Fed: 619..669 × 984..996; Borg: 620..676 × 988..1002).
    rl, rt, rr, rb = cfg.get("rarity_bbox", (619, 984, 669, 996))
    font_rarity = gfont(F_FUTURA_BOLD, PT_RARITY)
    rarity_text = format_rarity(ROW["CollectorsInfo"])
    tw = int(draw.textlength(rarity_text, font=font_rarity))
    r_y = vcenter_y(S(rt), S(rb - rt), font_rarity, rarity_text)
    cx = (S(rl) + S(rr)) // 2
    draw.text((cx - tw // 2, r_y), rarity_text, font=font_rarity, fill=BLACK)

    draw_disclaimer(canvas)
    return canvas


def draw_disclaimer(canvas):
    """Small white text rotated 90° CCW, running up the right edge — shared by
    every card type (it sits on the grey chrome strip outside the text box)."""
    disclaimer = "NOT ENDORSED BY CBS OR PAR. PIC."
    font_disclaimer = gfont(F_FUTURA_BOLD, 12)
    tw = int(font_disclaimer.getlength(disclaimer))
    asc, desc = font_disclaimer.getmetrics()
    th = asc + desc
    strip = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    # Mid-grey + reduced alpha so the text reads as a faint stencil
    ImageDraw.Draw(strip).text((0, 0), disclaimer, font=font_disclaimer, fill=(170, 170, 170, 130))
    strip_rot = strip.rotate(90, expand=True)
    # Centre within the grey chrome strip it sits on: horizontally between the
    # text-box right edge (~x=686) and the black border (~x=702), and vertically
    # within the strip's run (measured y≈637..969 on the rendered card).
    disclaimer_x = S(694) - strip_rot.width // 2
    disclaimer_y = (S(637) + S(969)) // 2 - strip_rot.height // 2
    canvas.alpha_composite(strip_rot, dest=(disclaimer_x, disclaimer_y))


# ---------------------------------------------------------------------------
# Dilemma cards — a simpler layout than personnel/ships: photo, frame, a single
# type icon (chosen by the DilemmaType field), cost, name, the static "Dilemma"
# label, game text and rarity. No staffing, skills, attributes or species oval.
# Assets come from the dilemma template (extracted/dilemma/assets); the type
# icons were pulled per-layer because only one is visible in the shipped PSD.
# ---------------------------------------------------------------------------
# DilemmaType code -> type-icon asset. 'D' is dual (space + planet).
DILEMMA_TYPE_ICON = {
    'D': "Type/Space_Planet.png",
    'S': "Type/Space.png",
    'P': "Type/Planet.png",
}
PT_DILEMMA_LABEL = 30   # the "Dilemma" type label (design-space points)


def clean_dilemma_name(name: str) -> str:
    """Strip collection annotations from the data name, e.g. 'Agonizing
    Encounter *VP' -> 'Agonizing Encounter', 'Assassination Attempt (AC)' ->
    'Assassination Attempt'. Subtitled names ('Chula: The Chandra') are kept."""
    name = re.sub(r"\s*\*\w+$", "", name)        # virtual/promo marker
    name = re.sub(r"\s*\([A-Z]+\)$", "", name)   # printing annotation
    return name.strip()


def render_dilemma(ROW: dict, NAME: str) -> Image.Image:
    canvas = Image.new("RGBA", (S(BASE_W), S(BASE_H)), (0, 0, 0, 0))

    # 1. Black Border background
    paste_rgba(canvas, DILEMMA_ASSETS / "Affiliation/Black_Border.png", S(-2), S(-2))

    # 2. Character art: scale the full-card scan to canvas, crop the photo window
    photo_full = Image.open(find_photo(ROW)).convert("RGBA").resize((S(BASE_W), S(BASE_H)), Image.LANCZOS)
    canvas.alpha_composite(photo_full.crop((S(32), S(140), S(672), S(574))), dest=(S(32), S(140)))

    # 3. Frame: notched photo-border strip (Difference blend, like personnel
    # Layer_4) then the card-background frame.
    layer4 = scale_asset(Image.open(DILEMMA_ASSETS / "Affiliation/Layer_4.png").convert("RGBA"))
    apply_difference(canvas, layer4, (S(31), S(139)))
    paste_rgba(canvas, DILEMMA_ASSETS / "Affiliation/Layer_10.png", S(27), S(26))

    # 4. Type icon — chosen by DilemmaType (Type group bbox top-left is 33,40).
    dtype = ROW["DilemmaType"].strip().upper()
    icon_rel = DILEMMA_TYPE_ICON.get(dtype)
    if icon_rel:
        paste_rgba(canvas, DILEMMA_ASSETS / icon_rel, S(33), S(40))
    else:
        print(f"  ! unknown dilemma type {dtype!r}; no type icon drawn")

    draw = ImageDraw.Draw(canvas)
    BLACK = (0, 0, 0, 255)
    WHITE = (255, 255, 255, 255)

    draw_cost(draw, ROW["Cost"])

    # Name — black, wraps when long. Dilemmas never carry a unique dot (the
    # printed cards don't show one even when the data has Unique=Y).
    draw_card_name(canvas, draw, NAME, unique=False,
                   bar_top=72, bar_h=30, base_x=190, right_edge=670, color=BLACK)

    # "Dilemma" type label — bold, centred in [313, 551, 415, 581]
    font_lbl = gfont(F_FUTURA_BOLD, PT_DILEMMA_LABEL)
    lbl = "Dilemma"
    lbl_w = int(draw.textlength(lbl, font=font_lbl))
    lbl_y = vcenter_y(S(551), S(30), font_lbl, lbl)
    cx = (S(313) + S(415)) // 2
    draw.text((cx - lbl_w // 2, lbl_y), lbl, font=font_lbl, fill=BLACK)

    # Game text — flows in the text band; auto-shrinks to fit. No keyword line.
    draw_textflow(canvas, draw, gametext_runs(ROW["gametext"]),
                  [120, 670, 635, 797], BLACK, PT_GAME, min_size=15)

    # Rarity — centred in [619, 984, 669, 996]
    font_rarity = gfont(F_FUTURA_BOLD, PT_RARITY)
    rarity_text = format_rarity(ROW["CollectorsInfo"])
    tw = int(draw.textlength(rarity_text, font=font_rarity))
    r_y = vcenter_y(S(984), S(12), font_rarity, rarity_text)
    cx = (S(619) + S(669)) // 2
    draw.text((cx - tw // 2, r_y), rarity_text, font=font_rarity, fill=BLACK)

    draw_disclaimer(canvas)
    return canvas


# ---------------------------------------------------------------------------
# Event cards — same frame family as dilemmas (photo, notched Difference-blend
# photo strip, card-background frame, centred type label, game text, rarity),
# from the event template (extracted/event/assets). Differences from dilemmas:
# the type label reads "Event"; there is no per-card type icon (the event swirl
# is baked into the frame); and the Keywords field (e.g. "Recall: 1.",
# "Maneuver.") renders as a bold lead-in to the game text, as on the printed
# cards. The italic lore quote and keyword reminder text aren't in the card data
# so they are omitted.
# ---------------------------------------------------------------------------
PT_EVENT_LABEL = 30   # the "Event" type label (design-space points)
PT_INTERRUPT_LABEL = 30   # the "Interrupt" type label (same PSD size as event)
PT_EQUIPMENT_LABEL = 38   # the "Equipment" label is larger on the printed cards
PT_MISSION_REQ = 30       # mission Requirements text (centred, bold upright)
PT_MISSION_POINTS = 60    # white italic display digits in the chrome circle
PT_MISSION_SPAN = 36      # white italic digit in the small black disc
PT_MISSION_AFFIL_TEXT = 24  # "Any affiliation..." / "Federation Headquarters" italic line
# Unique dot asset is shared across card families (the federation template's
# Card_Name/Unique). It sits just left of the name; when present, the name
# slides right by ~17px to make room.
UNIQUE_DOT_ASSET = ASSETS / "Cost__Name__Title__and_Class_Race/Card_Name/Unique/Unique.png"
UNIQUE_DOT_OFFSET = 17   # design-space px to shift the name when unique


def _wrap_name_lines(name: str, font, max_w: int) -> list[str]:
    """Greedy word-wrap a card name within max_w (scaled px) using `font`."""
    lines, cur = [], ""
    for word in name.split():
        candidate = f"{cur} {word}" if cur else word
        if cur and font.getlength(candidate) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = candidate
    if cur:
        lines.append(cur)
    return lines


def draw_card_name(canvas, draw, NAME: str, unique: bool,
                   bar_top: int, bar_h: int, base_x: int, right_edge: int,
                   color, base_size: int = PT_NAME, min_size: int = 22):
    """Draw the card name in the Crillee italic font, wrapping to additional
    lines when long (printed cards do this on titles like 'How Would You Like
    a Trip to Romulus?'). Shrinks to min_size before allowing >2 lines. Bar
    coords are design-space; right_edge is design-space too. If unique, paste
    the unique dot just left of the name and shift the name right."""
    if unique:
        # Centre the dot vertically with the (first) line of name text.
        paste_rgba(canvas, UNIQUE_DOT_ASSET, S(base_x), S(bar_top + 7))
        name_x = S(base_x + UNIQUE_DOT_OFFSET)
    else:
        name_x = S(base_x)
    max_w = S(right_edge) - name_x

    size = base_size
    while size > min_size:
        font_name = gfont(F_CRILEE, size)
        lines = _wrap_name_lines(NAME, font_name, max_w)
        if len(lines) <= 2 and all(font_name.getlength(l) <= max_w for l in lines):
            break
        size -= 1
    else:
        font_name = gfont(F_CRILEE, size)
        lines = _wrap_name_lines(NAME, font_name, max_w)

    if len(lines) == 1:
        name_y = vcenter_y(S(bar_top), S(bar_h), font_name, lines[0])
        draw.text((name_x, name_y), lines[0], font=font_name, fill=color)
    else:
        # Multi-line: centre the wrapped block on the bar's midline so both
        # lines sit symmetrically inside the two visible horizontal frame lines
        # bounding the name bar. (Reference: the printed "How Would You Like a
        # Trip to Romulus?" — the 2-line block is centred between those frame
        # lines, not anchored to the top or bottom.)
        line_h = int(S(size) * 1.0)
        midline = S(bar_top) + S(bar_h) // 2
        for i, line in enumerate(lines):
            center_y = midline + (i - (len(lines) - 1) / 2) * line_h
            fb = font_name.getbbox(line)
            y = int(center_y - (fb[1] + fb[3]) / 2)
            draw.text((name_x, y), line, font=font_name, fill=color)


def render_event(ROW: dict, NAME: str) -> Image.Image:
    canvas = Image.new("RGBA", (S(BASE_W), S(BASE_H)), (0, 0, 0, 0))

    # 1. Black Border background
    paste_rgba(canvas, EVENT_ASSETS / "Affiliation/Black_Border.png", S(-2), S(-2))

    # 2. Character art: scale the full-card scan to canvas, crop the photo window
    photo_full = Image.open(find_photo(ROW)).convert("RGBA").resize((S(BASE_W), S(BASE_H)), Image.LANCZOS)
    canvas.alpha_composite(photo_full.crop((S(32), S(140), S(672), S(574))), dest=(S(32), S(140)))

    # 3. Frame: notched photo-border strip (Difference blend, like the dilemma /
    # personnel Layer_4) then the card-background frame (carries the event swirl).
    frame = scale_asset(Image.open(EVENT_ASSETS / "Affiliation/Frame.png").convert("RGBA"))
    apply_difference(canvas, frame, (S(31), S(139)))
    paste_rgba(canvas, EVENT_ASSETS / "Affiliation/Layer_13.png", S(27), S(26))

    draw = ImageDraw.Draw(canvas)
    BLACK = (0, 0, 0, 255)
    WHITE = (255, 255, 255, 255)

    draw_cost(draw, ROW["Cost"])

    # Name — black, left-aligned, wraps to a second line for long names; if
    # Unique=Y, paste the unique dot just left of the name. For card types
    # without a subtitle (events, dilemmas), the name midline matches the big
    # affiliation swirl's vertical centre (~y=80), not the cost circle. The
    # swirl runs canvas y≈30..130 in the event Layer_13 asset.
    draw_card_name(canvas, draw, NAME, ROW["Unique"].upper() == "Y",
                   bar_top=65, bar_h=30, base_x=189, right_edge=670, color=BLACK)

    # "Event" type label — bold, centred in [332, 554, 396, 580]
    font_lbl = gfont(F_FUTURA_BOLD, PT_EVENT_LABEL)
    lbl = "Event"
    lbl_w = int(draw.textlength(lbl, font=font_lbl))
    lbl_y = vcenter_y(S(554), S(26), font_lbl, lbl)
    cx = (S(332) + S(396)) // 2
    draw.text((cx - lbl_w // 2, lbl_y), lbl, font=font_lbl, fill=BLACK)

    # Game text — keyword (e.g. "Recall: 1.") renders bold ahead of the rules
    # text, in one wrapped flow. Auto-shrinks to fit; lore quote omitted (not in
    # the data), so the box reaches down into the lore band.
    keywords_text = strip_braces(ROW["Keywords"].strip())
    runs = gametext_runs(ROW["gametext"])
    if keywords_text:
        runs = [(keywords_text + " ", 'bold')] + runs
    draw_textflow(canvas, draw, runs, [120, 672, 639, 828], BLACK, PT_GAME, min_size=15)

    # Rarity — centred in [619, 984, 669, 996]
    font_rarity = gfont(F_FUTURA_BOLD, PT_RARITY)
    rarity_text = format_rarity(ROW["CollectorsInfo"])
    tw = int(draw.textlength(rarity_text, font=font_rarity))
    r_y = vcenter_y(S(984), S(12), font_rarity, rarity_text)
    cx = (S(619) + S(669)) // 2
    draw.text((cx - tw // 2, r_y), rarity_text, font=font_rarity, fill=BLACK)

    draw_disclaimer(canvas)
    return canvas


# ---------------------------------------------------------------------------
# Interrupt cards — same frame family as events (photo, notched Difference-blend
# photo strip, card-background frame, centred type label, game text, rarity),
# from the interrupt template (extracted/interrupt/assets). Differences from
# events: assets live under INTERRUPT_ASSETS and the card-background layer is
# Layer_16; the type label reads "Interrupt"; per reconstruct_card.md printed
# interrupts never carry the unique dot, so unique=False regardless of the data
# field. Italic lore quote and keyword reminder text aren't in the card data so
# they are omitted.
# ---------------------------------------------------------------------------
def render_interrupt(ROW: dict, NAME: str) -> Image.Image:
    canvas = Image.new("RGBA", (S(BASE_W), S(BASE_H)), (0, 0, 0, 0))

    paste_rgba(canvas, INTERRUPT_ASSETS / "Affiliation/Black_Border.png", S(-2), S(-2))

    photo_full = Image.open(find_photo(ROW)).convert("RGBA").resize((S(BASE_W), S(BASE_H)), Image.LANCZOS)
    canvas.alpha_composite(photo_full.crop((S(32), S(140), S(672), S(574))), dest=(S(32), S(140)))

    frame = scale_asset(Image.open(INTERRUPT_ASSETS / "Affiliation/Frame.png").convert("RGBA"))
    apply_difference(canvas, frame, (S(31), S(139)))
    paste_rgba(canvas, INTERRUPT_ASSETS / "Affiliation/Layer_16.png", S(27), S(26))

    draw = ImageDraw.Draw(canvas)
    BLACK = (0, 0, 0, 255)

    # Interrupts have no cost symbol on the printed card (the data's Cost
    # column is meaningless here) and never show the unique dot.
    draw_card_name(canvas, draw, NAME, unique=False,
                   bar_top=65, bar_h=30, base_x=189, right_edge=670, color=BLACK)

    # "Interrupt" type label — bold, centred in [313, 555, 417, 587]
    font_lbl = gfont(F_FUTURA_BOLD, PT_INTERRUPT_LABEL)
    lbl = "Interrupt"
    lbl_w = int(draw.textlength(lbl, font=font_lbl))
    lbl_y = vcenter_y(S(555), S(32), font_lbl, lbl)
    cx = (S(313) + S(417)) // 2
    draw.text((cx - lbl_w // 2, lbl_y), lbl, font=font_lbl, fill=BLACK)

    # Game text — keyword (rare for interrupts) renders bold ahead of the
    # rules text, in one wrapped flow; auto-shrinks to fit.
    keywords_text = strip_braces(ROW["Keywords"].strip())
    runs = gametext_runs(ROW["gametext"])
    if keywords_text:
        runs = [(keywords_text + " ", 'bold')] + runs
    draw_textflow(canvas, draw, runs, [120, 672, 639, 796], BLACK, PT_GAME, min_size=15)

    # Rarity — centred in [619, 984, 669, 996]
    font_rarity = gfont(F_FUTURA_BOLD, PT_RARITY)
    rarity_text = format_rarity(ROW["CollectorsInfo"])
    tw = int(draw.textlength(rarity_text, font=font_rarity))
    r_y = vcenter_y(S(984), S(12), font_rarity, rarity_text)
    cx = (S(619) + S(669)) // 2
    draw.text((cx - tw // 2, r_y), rarity_text, font=font_rarity, fill=BLACK)

    draw_disclaimer(canvas)
    return canvas


# ---------------------------------------------------------------------------
# Equipment cards — same frame family as events/interrupts (photo + notched
# Difference-blend strip + card-background frame + centred type label + game
# text + rarity), from the equipment template (extracted/equipment/assets).
# Differences: card-background layer is Layer_15; the type label reads
# "Equipment" and is set noticeably larger than Event/Interrupt; the game-text
# box is shorter (bottom y=734 vs event's 796), leaving room for a lore quote
# (which we don't render). Equipment shows the unique dot per
# reconstruct_card.md. The PSD ships an empty placeholder Layer_14 over the
# photo window — it has no pixels, so we skip it.
# ---------------------------------------------------------------------------
def render_equipment(ROW: dict, NAME: str) -> Image.Image:
    canvas = Image.new("RGBA", (S(BASE_W), S(BASE_H)), (0, 0, 0, 0))

    paste_rgba(canvas, EQUIPMENT_ASSETS / "Affiliation/Black_Border.png", S(-2), S(-2))

    photo_full = Image.open(find_photo(ROW)).convert("RGBA").resize((S(BASE_W), S(BASE_H)), Image.LANCZOS)
    canvas.alpha_composite(photo_full.crop((S(32), S(140), S(672), S(574))), dest=(S(32), S(140)))

    frame = scale_asset(Image.open(EQUIPMENT_ASSETS / "Affiliation/Frame.png").convert("RGBA"))
    apply_difference(canvas, frame, (S(31), S(139)))
    paste_rgba(canvas, EQUIPMENT_ASSETS / "Affiliation/Layer_15.png", S(27), S(26))

    draw = ImageDraw.Draw(canvas)
    BLACK = (0, 0, 0, 255)

    draw_cost(draw, ROW["Cost"])

    draw_card_name(canvas, draw, NAME, ROW["Unique"].upper() == "Y",
                   bar_top=65, bar_h=30, base_x=190, right_edge=670, color=BLACK)

    # "Equipment" type label — bold, centred in [305, 555, 424, 589]
    font_lbl = gfont(F_FUTURA_BOLD, PT_EQUIPMENT_LABEL)
    lbl = "Equipment"
    lbl_w = int(draw.textlength(lbl, font=font_lbl))
    lbl_y = vcenter_y(S(555), S(34), font_lbl, lbl)
    cx = (S(305) + S(424)) // 2
    draw.text((cx - lbl_w // 2, lbl_y), lbl, font=font_lbl, fill=BLACK)

    # Game text — keyword bold lead-in, then rules text in one flow.
    keywords_text = strip_braces(ROW["Keywords"].strip())
    runs = gametext_runs(ROW["gametext"])
    if keywords_text:
        runs = [(keywords_text + " ", 'bold')] + runs
    # The PSD's game-text bbox stops at y=734 because the template reserves
    # space for a lore quote below; we don't render lore, so the band extends
    # down through the lore area (same pattern as render_event).
    draw_textflow(canvas, draw, runs, [120, 671, 650, 828], BLACK, PT_GAME, min_size=15)

    # Rarity — centred in [619, 984, 669, 996]
    font_rarity = gfont(F_FUTURA_BOLD, PT_RARITY)
    rarity_text = format_rarity(ROW["CollectorsInfo"])
    tw = int(draw.textlength(rarity_text, font=font_rarity))
    r_y = vcenter_y(S(984), S(12), font_rarity, rarity_text)
    cx = (S(619) + S(669)) // 2
    draw.text((cx - tw // 2, r_y), rarity_text, font=font_rarity, fill=BLACK)

    draw_disclaimer(canvas)
    return canvas


# ---------------------------------------------------------------------------
# Mission cards — landscape-art portrait layout from extracted/mission/.
# The PSD's Affiliations/{Even,Odd}/*.png assets are empty (the printed glyphs
# came entirely from layer styles, which don't rasterise), so the affiliation
# icons are composited from extracted/icons/inline/*.png upscaled to the
# strip height. Mission column drives the type icon (S/P/H); Quadrant column
# drives the quadrant icon (A/G/D; B = no icon). Skills column carries the
# requirements line. Lore (italic quote) isn't in our data — skip.
# ---------------------------------------------------------------------------
MISSION_TYPE_ASSET = {
    "S": "Type/Space.png",
    "P": "Type/Planet.png",
    "H": "Type/Headquarters.png",
}
MISSION_QUADRANT_ASSET = {
    "A": "Quadrant/Alpha.png",
    "G": "Quadrant/Gamma.png",
    "D": "Quadrant/Delta.png",
    # B (Beta) has no icon in the template — Beta is the implicit/default.
}
# Map data-field affiliation abbreviations -> baked affiliation-icon filename.
# Icons are the centre disc of the PSD's Odd/<affil> row, with the strip's
# navy background keyed out — they include the chrome ring and laurel wreath.
MISSION_AFFIL_ICON = {
    "Fed": "Federation",
    "NA":  "Non-Alligned",
    "Kli": "Klingon",
    "Rom": "Romulan",
    "Car": "Cardassian",
    "Dom": "Dominion",
    "Baj": "Bajoran",
    "Fer": "Ferengi",
    "Bor": "Borg",
}

# Affiliation strip geometry, derived from the PSD's actual icon positions in
# the Odd (5-icon row) and Even (6-icon row) layers. For N affiliations the
# printed cards use the centre N positions of whichever row has the matching
# parity. For N>6 the strip is too narrow for natural-size icons, so we
# equally space N icons across the strip and scale them down.
MISSION_AFFIL_CY = 895
MISSION_AFFIL_ODD_X  = (169, 268, 367, 466, 564)
MISSION_AFFIL_EVEN_X = (120, 218, 316, 414, 512, 610)
MISSION_AFFIL_NATIVE_PX = 100   # baked icon width in design space

def _affil_positions(n: int):
    """Return (x_centres, scale) for N affiliation icons."""
    if n <= 0:
        return (), 1.0
    if n <= 5 and n % 2 == 1:
        row = MISSION_AFFIL_ODD_X
        start = (len(row) - n) // 2
        return row[start:start + n], 1.0
    if n <= 6 and n % 2 == 0:
        row = MISSION_AFFIL_EVEN_X
        start = (len(row) - n) // 2
        return row[start:start + n], 1.0
    # N=7+: even-space across the strip and shrink each icon so they don't
    # overlap. The Even row's full span (120..610) is the strip's working
    # width; divide it into N slots and centre each icon in its slot.
    left, right = MISSION_AFFIL_EVEN_X[0], MISSION_AFFIL_EVEN_X[-1]
    width = right - left
    slot = width / (n - 1) if n > 1 else 0
    positions = tuple(int(round(left + i * slot)) for i in range(n))
    natural_slot = (MISSION_AFFIL_EVEN_X[1] - MISSION_AFFIL_EVEN_X[0])
    scale = min(1.0, slot / natural_slot) if slot else 1.0
    return positions, scale


def _paste_affil_icon(canvas, stem: str, cx: int, cy: int, scale: float = 1.0):
    """Paste a baked mission affiliation icon centred at (cx, cy) in design
    space, optionally pre-scaled (for N>=7 layouts that shrink to fit)."""
    src = Image.open(
        MISSION_ASSETS / "Affiliations/icons" / f"{stem}.png").convert("RGBA")
    if scale != 1.0:
        new_w = max(1, int(round(src.width * scale)))
        new_h = max(1, int(round(src.height * scale)))
        src = src.resize((new_w, new_h), Image.LANCZOS)
    src = scale_asset(src)
    canvas.alpha_composite(src,
        dest=(S(cx) - src.width // 2, S(cy) - src.height // 2))


def draw_centered_text(draw, text: str, font, box, fill):
    """Wrap `text` to fit box width and draw centred horizontally, anchored to
    the top of the box. box=[l,t,r,b] in design space."""
    l, t, r, _ = (S(box[0]), S(box[1]), S(box[2]), S(box[3]))
    max_w = r - l
    # Greedy wrap by word
    words = text.split()
    lines = []
    cur = ""
    for w in words:
        trial = (cur + " " + w).strip()
        if int(draw.textlength(trial, font=font)) <= max_w:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)

    # Use font size for line height
    line_h = int(font.size * 1.15)
    y = t
    for line in lines:
        tw = int(draw.textlength(line, font=font))
        cx = (l + r) // 2
        draw.text((cx - tw // 2, y), line, font=font, fill=fill)
        y += line_h
    return y


def render_mission(ROW: dict, NAME: str, TITLE: str = "") -> Image.Image:
    canvas = Image.new("RGBA", (S(BASE_W), S(BASE_H)), (0, 0, 0, 0))

    # 1. Black border background
    paste_rgba(canvas, MISSION_ASSETS / "Black_Border.png", S(-2), S(-2))

    # 2. Photo: scale full-card scan to canvas; crop landscape art window
    # (the Image_Frame layer bbox in the mission spec.json).
    photo_full = Image.open(find_photo(ROW)).convert("RGBA").resize(
        (S(BASE_W), S(BASE_H)), Image.LANCZOS)
    canvas.alpha_composite(photo_full.crop((S(33), S(138), S(708), S(586))),
                           dest=(S(33), S(138)))

    # 3. Card frame (chrome with affiliation circle holes baked in).
    paste_rgba(canvas, MISSION_ASSETS / "Card_Background/Card_Frame.png",
               S(27), S(26))

    # 4. Type icon (Space/Planet/Headquarters) top-left.
    mtype = (ROW.get("Mission") or "").strip().upper()
    if mtype in MISSION_TYPE_ASSET:
        paste_rgba(canvas, MISSION_ASSETS / MISSION_TYPE_ASSET[mtype],
                   S(33), S(40))

    # 5. Quadrant icon (left, below image). Beta has no asset.
    quad = (ROW.get("Quadrant") or "").strip().upper()
    if quad in MISSION_QUADRANT_ASSET:
        paste_rgba(canvas, MISSION_ASSETS / MISSION_QUADRANT_ASSET[quad],
                   S(33), S(552))

    draw = ImageDraw.Draw(canvas)
    BLACK = (0, 0, 0, 255)
    WHITE = (255, 255, 255, 255)

    # 6. Card name (location, in the top chrome bar) + title (objective, in
    # the italic sub-band underneath). Unique dot prefixes the name.
    # PSD bbox: Name [182, 54, 310, 79], Title [180, 93, 309, 112].
    draw_card_name(canvas, draw, NAME, ROW.get("Unique", "").upper() == "Y",
                   bar_top=54, bar_h=30, base_x=180, right_edge=665, color=BLACK)
    if TITLE:
        font_title = gfont(F_CRILEE, PT_TITLE)
        title_y = vcenter_y(S(93), S(22), font_title, TITLE)
        # Left-aligned with the name (which sits at base_x + unique-dot offset
        # when unique). The two lines read as a single block.
        title_x = S(180 + UNIQUE_DOT_OFFSET) \
                  if ROW.get("Unique", "").upper() == "Y" else S(180)
        draw.text((title_x, title_y), TITLE, font=font_title, fill=BLACK)

    # 7. Points — white heavy display digit, centred in the chrome circle at
    # top-right of the below-image bar. PSD bbox: [628, 568, 701, 613]; the
    # actual printed glyphs are much taller than the bbox (they overflow into
    # the chrome ring) and use an ultra-compressed weight, so use Swiss 911
    # UCm and treat the bbox centre as the anchor, not the height bound.
    points_text = (ROW.get("Points") or "").strip()
    if points_text:
        # Italic Crillee matches the printed Points/Span digits.
        font_pts = gfont(F_CRILEE, PT_MISSION_POINTS)
        l, t, r, b = font_pts.getbbox(points_text)
        cx_design, cy_design = 664, 590       # circle centre, design space
        px = S(cx_design) - (l + r) // 2
        py = S(cy_design) - (t + b) // 2
        draw.text((px, py), points_text, font=font_pts, fill=WHITE)

    # 8. Requirements (Skills column) — centred bold upright, may wrap.
    req_text = (ROW.get("Skills") or "").strip()
    if req_text:
        font_req = gfont(F_FUTURA_BOLD, PT_MISSION_REQ)
        # PSD bbox ([163, 612, 566, 675]) is too narrow (text spills to many
        # lines), but the printed cards wrap "Astrometrics, Engineer,
        # Physics," to line 1 — pick a width between that line's measured
        # length and the next candidate ("...Cunning>34,") so it breaks here.
        draw_centered_text(draw, req_text, font_req,
                           [135, 612, 605, 685], BLACK)

    # 9. Game text — keyword bold lead-in, then rules text in one flow.
    keywords_text = strip_braces((ROW.get("Keywords") or "").strip())
    runs = gametext_runs(ROW.get("gametext", ""))
    if keywords_text:
        runs = [(keywords_text + " ", 'bold')] + runs
    # PSD game-text bbox is [73, 689, 543, 783], but we don't render the lore
    # quote, so widen right edge to the affiliation strip top and extend down
    # through the lore band.
    draw_textflow(canvas, draw, runs, [73, 689, 660, 828], BLACK,
                  PT_GAME, min_size=15)

    # 10. Affiliation strip. Data values:
    #     "[Fed]"       -> one inline icon centred
    #     "[Fed][NA]"   -> two icons split across the strip
    #     "Any affiliation may attempt this mission." -> render as italic text
    #     "<x> Headquarters"                          -> render as italic text
    affil = (ROW.get("Affiliation") or "").strip()
    affil_tokens = re.findall(r"\[([^\]]+)\]", affil)
    if affil_tokens:
        stems = [MISSION_AFFIL_ICON.get(a) for a in affil_tokens]
        stems = [s for s in stems if s]
        positions, scale = _affil_positions(len(stems))
        for stem, cx in zip(stems, positions):
            _paste_affil_icon(canvas, stem, cx, MISSION_AFFIL_CY, scale)
    elif affil:
        # Plain text variant (Any / HQ). Italic bold.
        font_aff = gfont(F_FUTURA_BOLDO, PT_MISSION_AFFIL_TEXT)
        # Strip wraps PSD bbox roughly [80, 875, 655, 915].
        tw = int(draw.textlength(affil, font=font_aff))
        y = vcenter_y(S(875), S(40), font_aff, affil)
        cx = (S(80) + S(655)) // 2
        draw.text((cx - tw // 2, y), affil, font=font_aff, fill=BLACK)

    # 11. Span — white digit centred in the small black disc at the bottom.
    # PSD bbox: [360, 953, 373, 982]. Same Futuri Condensed Bold face as Points.
    span_text = (ROW.get("Span") or "").strip()
    if span_text:
        # Italic Crillee, centred on the small black disc (canvas ≈ 367, 968).
        font_span = gfont(F_CRILEE, PT_MISSION_SPAN)
        l, t, r, b = font_span.getbbox(span_text)
        # Span disc centre measured from Card_Frame.png by darkness scan.
        cx_design, cy_design = 365, 968
        sx = S(cx_design) - (l + r) // 2
        sy = S(cy_design) - (t + b) // 2
        draw.text((sx, sy), span_text, font=font_span, fill=WHITE)

    # 12. Rarity — bottom right (same band as event/equipment).
    font_rarity = gfont(F_FUTURA_BOLD, PT_RARITY)
    rarity_text = format_rarity(ROW["CollectorsInfo"])
    tw = int(draw.textlength(rarity_text, font=font_rarity))
    r_y = vcenter_y(S(982), S(14), font_rarity, rarity_text)
    cx = (S(624) + S(665)) // 2
    draw.text((cx - tw // 2, r_y), rarity_text, font=font_rarity, fill=BLACK)

    draw_disclaimer(canvas)
    return canvas


# ---------------------------------------------------------------------------
# Batch driver
# ---------------------------------------------------------------------------

def parse_args(argv):
    """Minimal parser: optional positional INPUT and `--dpi N` (default 300)."""
    global SCALE
    input_path, dpi = DEFAULT_INPUT, BASE_DPI
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--dpi":
            i += 1
            dpi = int(argv[i])
        elif a.startswith("--dpi="):
            dpi = int(a.split("=", 1)[1])
        else:
            input_path = Path(a)
        i += 1
    if dpi not in SUPPORTED_DPI:
        print(f"Note: --dpi {dpi} is outside the tested set {SUPPORTED_DPI}; rendering anyway.")
    SCALE = dpi / BASE_DPI
    return input_path, dpi


def main():
    input_path, dpi = parse_args(sys.argv[1:])
    if not input_path.exists():
        raise SystemExit(f"Input file not found: {input_path}")

    rows = load_rows(input_path)
    overrides = load_name_title_overrides(NAME_TITLE_OVERRIDE)
    bold_markup = load_gametext_markup(GAMETEXT_MARKUP)
    for row in rows:
        if row["CollectorsInfo"] in bold_markup:
            row["gametext"] = bold_markup[row["CollectorsInfo"]]
    outdir = OUTDIR if dpi == BASE_DPI else OUTDIR / f"{dpi}dpi"
    outdir.mkdir(parents=True, exist_ok=True)

    # OCR (NAME/TITLE split) is only needed for non-dilemma cards without a
    # sidecar override; dilemmas use their data name verbatim.
    needs_ocr = any(r["Type"].strip().lower() not in
                    ("dilemma", "event", "interrupt", "equipment")
                    and r["CollectorsInfo"] not in overrides for r in rows)
    if needs_ocr and shutil.which("tesseract") is None:
        raise SystemExit("tesseract CLI not found on PATH (needed for NAME/TITLE "
                         "OCR). Install it or pin cards via " + str(NAME_TITLE_OVERRIDE))

    print(f"Rendering {len(rows)} card(s) from {input_path} at {dpi} DPI (scale {SCALE:.3f})")
    rendered = 0
    low_conf = []
    for row in rows:
        cid = row["CollectorsInfo"]
        try:
            if row["Type"].strip().lower() == "dilemma":
                name = clean_dilemma_name(row["Name"])
                print(f"  {cid}: dilemma NAME={name!r}")
                canvas = render_dilemma(row, name)
                out = outdir / f"{cid}.png"
                canvas.save(out, dpi=(dpi, dpi))
                rendered += 1
                continue
            if row["Type"].strip().lower() == "event":
                name = clean_dilemma_name(row["Name"])
                print(f"  {cid}: event NAME={name!r}")
                canvas = render_event(row, name)
                out = outdir / f"{cid}.png"
                canvas.save(out, dpi=(dpi, dpi))
                rendered += 1
                continue
            if row["Type"].strip().lower() == "interrupt":
                name = clean_dilemma_name(row["Name"])
                print(f"  {cid}: interrupt NAME={name!r}")
                canvas = render_interrupt(row, name)
                out = outdir / f"{cid}.png"
                canvas.save(out, dpi=(dpi, dpi))
                rendered += 1
                continue
            if row["Type"].strip().lower() == "equipment":
                name = clean_dilemma_name(row["Name"])
                print(f"  {cid}: equipment NAME={name!r}")
                canvas = render_equipment(row, name)
                out = outdir / f"{cid}.png"
                canvas.save(out, dpi=(dpi, dpi))
                rendered += 1
                continue
            if row["Type"].strip().lower() == "mission":
                full = clean_dilemma_name(row["Name"])
                if cid in overrides:
                    name, title = overrides[cid]
                    print(f"  {cid}: mission NAME={name!r} TITLE={title!r} (sidecar override)")
                else:
                    photo = find_photo(row)
                    name, title, score, margin = infer_name_title(
                        full, photo, MISSION_NAME_ROW_BOX)
                    confident = score >= OCR_MIN_SCORE or margin >= OCR_MIN_MARGIN
                    if not confident:
                        low_conf.append(cid)
                    flag = "" if confident else "  <-- LOW CONFIDENCE, verify"
                    print(f"  {cid}: mission NAME={name!r} TITLE={title!r} "
                          f"(OCR score {score:.2f}, margin {margin:.2f}){flag}")
                canvas = render_mission(row, name, title)
                out = outdir / f"{cid}.png"
                canvas.save(out, dpi=(dpi, dpi))
                rendered += 1
                continue
            if cid in overrides:
                name, title = overrides[cid]
                print(f"  {cid}: NAME={name!r} TITLE={title!r} (sidecar override)")
            else:
                photo = find_photo(row)
                name, title, score, margin = infer_name_title(row["Name"], photo)
                confident = score >= OCR_MIN_SCORE or margin >= OCR_MIN_MARGIN
                flag = "" if confident else "  <-- LOW CONFIDENCE, verify"
                if not confident:
                    low_conf.append(cid)
                print(f"  {cid}: NAME={name!r} TITLE={title!r} "
                      f"(OCR score {score:.2f}, margin {margin:.2f}){flag}")
            canvas = render_card(row, name, title)
        except SystemExit as e:
            print(f"  ! skipping {cid}: {e}")
            continue
        out = outdir / f"{cid}.png"
        canvas.save(out, dpi=(dpi, dpi))
        rendered += 1

    print(f"Done: {rendered}/{len(rows)} card(s) rendered into {outdir} "
          f"({S(BASE_W)}x{S(BASE_H)} px)")
    if low_conf:
        print(f"Low-confidence NAME/TITLE split for: {', '.join(low_conf)} "
              f"-- verify, and pin in {NAME_TITLE_OVERRIDE} if wrong.")


if __name__ == "__main__":
    main()
