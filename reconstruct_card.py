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
FONTS  = Path("fonts")
PHOTOS = Path("fixture/low quality decipher images")
NAME_TITLE_OVERRIDE = Path("fixture/name_title_map.tsv")
DEFAULT_INPUT  = Path("fixture/federation_personnel_fixture.txt")
OUTDIR = Path("fixture/reconstructed")

# NAME/TITLE split inference via OCR of the card's top (name) row.
NAME_ROW_BOX = (92, 26, 340, 46)   # name-row band in the ~357x499 Decipher scan
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
    """Parse a tab-separated card file, skipping the header row."""
    rows = []
    with path.open() as f:
        for line in f:
            parts = [p.strip('"') for p in line.rstrip("\n").split("\t")]
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


def find_photo(row: dict) -> Path:
    for stem in (row["CollectorsInfo"], row["ImageFile"]):
        p = PHOTOS / f"{stem}.jpg"
        if p.exists():
            return p
    raise SystemExit(f"No photo found for {row['CollectorsInfo']}")


def _ocr_name_row(photo: Path) -> str:
    """OCR the top (name) row band of a card scan."""
    OCR_TMP.mkdir(exist_ok=True)
    im = Image.open(photo).convert("L")
    l, t, r, b = NAME_ROW_BOX
    crop = im.crop((l, t, r, b)).resize(((r - l) * OCR_UPSCALE, (b - t) * OCR_UPSCALE))
    out = OCR_TMP / "name_row.png"
    crop.save(out)
    res = subprocess.run(["tesseract", str(out), "stdout", "--psm", "7"],
                         capture_output=True, text=True)
    return res.stdout.strip()


def _norm_words(s: str) -> list:
    return re.sub(r"[^a-z0-9 ]", " ", s.lower()).split()


def infer_name_title(full_name: str, photo: Path):
    """Split the concatenated column-1 name into (name, title) by OCR'ing the
    card's name row and fuzzy-aligning it against the known tokens. The OCR text
    is usually garbled, but we only need the boundary, so we score each candidate
    split point and take the best. Returns (name, title, score, margin)."""
    tokens = full_name.split()
    if len(tokens) <= 1:
        return full_name, "", 1.0, 1.0
    ocr = " ".join(_norm_words(_ocr_name_row(photo)))
    scored = sorted(
        ((SequenceMatcher(None, " ".join(_norm_words(" ".join(tokens[:k]))), ocr).ratio(), k)
         for k in range(1, len(tokens))),
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
# Icon sockets for slots 1 & 3 — only drawn under slots that actually have an
# icon. (Slot 2's socket is baked into Layer_11.)
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
    'AU':   ('slot3', "Personnel/Staffing/Slot_3/AU.png"),
    'Past': ('slot3', "Personnel/Staffing/Slot_3/Past.png"),
}

# Each slot's ring CENTRE on the card canvas (47px ring → centre is paste+23).
# Slot 2 was historically pasted as a 47×47 image at (49, 697), so its ring
# centre is (49+23, 697+23) = (72, 720). The other slots match the same
# vertical rhythm (67px apart from slot 2 ring centre, per spec measurements).
SLOT_RING_CENTRE = {
    'slot1': (72, 653),   # was paste (49, 630) + 23
    'slot2': (72, 720),   # was paste (49, 697) + 23
    'slot3': (72, 797),   # was paste (49, 774) + 23
}


def render_icons(canvas, icons_str):
    """Paste sockets (slots 1 & 3) and icons. Slot 2's socket is in Layer_11."""
    filled_slots = set()
    for abbrev in re.findall(r'\[([^\]]+)\]', icons_str):
        entry = ICON_MAP.get(abbrev)
        if not entry:
            print(f"  ! unknown icon: [{abbrev}]")
            continue
        slot, _ = entry
        filled_slots.add(slot)

    # The socket plate's bright chrome connector sits on its top edge only;
    # slot 2's baked socket connects both up and down. Mirror the plate onto
    # itself (pixel-wise lighten) so slots 1 & 3 get a bright connector at top
    # and bottom to match the middle slot.
    socket = scale_asset(Image.open(SOCKET_ASSET).convert("RGBA"))
    socket = ImageChops.lighter(socket, ImageOps.flip(socket))
    for slot in ("slot1", "slot3"):
        if slot in filled_slots:
            cx, cy = SLOT_RING_CENTRE[slot]
            canvas.alpha_composite(socket, dest=(S(cx) - S(SOCKET_CENTRE[0]), S(cy) - S(SOCKET_CENTRE[1])))

    for abbrev in re.findall(r'\[([^\]]+)\]', icons_str):
        entry = ICON_MAP.get(abbrev)
        if not entry:
            continue
        slot, rel = entry
        asset_path = ASSETS / "Staffing_and_Attributes" / rel
        cx, cy = SLOT_RING_CENTRE[slot]
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
SHIP_STAFF_ASSETS = ASSETS / "Staffing_and_Attributes/Ship/Staffing"
# The template only carries a populated icon for the slots that were filled on
# the source PSD's example ship (Command in slot 1, Staff in slots 2-4); the
# other per-slot layers are empty. Since the staffing column sits *inside* the
# photo window, an empty slot lets the low-res scan's own printed icon show
# through (blurry, no socket ring, misaligned). So use one canonical crisp icon
# per kind for every slot — it fully occludes the photo and stays aligned.
SHIP_STAFF_ICON = {
    'Cmd': SHIP_STAFF_ASSETS / "Slot_1/Command.png",
    'Stf': SHIP_STAFF_ASSETS / "Slot_2/Staff.png",
}
# Design-space top-left of each slot's 49x49 socket (from spec.json bboxes).
SHIP_STAFF_SLOT_XY = [(48, 168), (48, 223), (48, 279), (48, 334), (48, 389)]


def render_ship_staffing(canvas, staff_str):
    """Paste the vertical staffing-requirement icons for a ship from its Staff
    field. Brackets map top-to-bottom onto slots 1..5.

    The icon includes its dark socket disc (authentic — the printed card has a
    dark button behind each star, see the source scans), which also occludes the
    low-res scan's own printed star beneath. A transparent-disc star would let
    that scan star ghost through, slightly offset, so the disc stays."""
    abbrevs = re.findall(r'\[([^\]]+)\]', staff_str)
    for idx, abbrev in enumerate(abbrevs):
        if idx >= len(SHIP_STAFF_SLOT_XY):
            print(f"  ! more than {len(SHIP_STAFF_SLOT_XY)} staffing icons; ignoring extras")
            break
        asset_path = SHIP_STAFF_ICON.get(abbrev)
        if not asset_path:
            print(f"  ! unknown ship staffing icon: [{abbrev}]")
            continue
        x, y = SHIP_STAFF_SLOT_XY[idx]
        paste_rgba(canvas, asset_path, S(x), S(y))


# ---------------------------------------------------------------------------
# Card font point sizes (design space). Derived from PDF: PDF_pts × (300/72) =
# PIL px @ 300 DPI. Confirmed against Benjamin Sisko in fixture/2eed_hires.pdf.
# Loaded per render via gfont() so they scale with the output DPI.
# ---------------------------------------------------------------------------
PT_COST, PT_NAME, PT_TITLE, PT_SPECIES = 33, 35, 27, 33
PT_SKILL, PT_GAME, PT_ATTR, PT_RARITY = 29, 29, 28, 17
PT_ATTR_CAP, PT_ATTR_SC = 28, 21


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
    'Cmd': 'command', 'Stf': 'staff',
    'TNG': 'tng', 'DS9': 'ds9', 'Voy': 'voyager', 'TOS': 'tos',
    'Maq': 'maquis', 'E': 'earth',
    'Fut': 'future', 'AU': 'au', 'Past': 'past',
    'Fed': 'federation', 'NA': 'nonaligned', 'Fer': 'ferengi',
    'AQ': 'quadrant_alpha', 'GQ': 'quadrant_gamma', 'DQ': 'quadrant_delta',
    'Dual': 'dual', 'HQ': 'headquarters',
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


def gametext_runs(text):
    """Styled runs for game text: a leading 'Order -'-style lexeme is bold,
    the remainder is medium."""
    text = strip_braces(text.strip())
    m = re.match(r'^([A-Z][A-Za-z]+ -)(.*)$', text)
    if m:
        return [(m.group(1), 'bold'), (m.group(2), 'med')]
    return [(text, 'med')]


def keyword_runs(text):
    """Styled runs for the lore/keyword line: type before ': ' is bold, the
    value after it is italic."""
    text = strip_braces(text.strip())
    if ': ' in text:
        head, tail = text.split(': ', 1)
        return [(head + ': ', 'bold'), (tail, 'italic')]
    return [(text, 'bold')]


def _flow_tokens(styled_runs, size):
    """Tokenise styled runs at a given font size into (kind, payload, width, font)."""
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
    return tokens


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
            x += tw
        y += line_h
    return y


# ---------------------------------------------------------------------------
# Build canvas — spec.json order is bottom-to-top (index 0 = bottommost)
# ---------------------------------------------------------------------------

def render_card(ROW: dict, NAME: str, TITLE: str) -> Image.Image:
    is_ship = ROW["Type"].strip().lower() == "ship"

    canvas = Image.new("RGBA", (S(BASE_W), S(BASE_H)), (0, 0, 0, 0))

    # 1. Black Border — background, provides outer card colour
    paste_rgba(canvas, ASSETS / "Black_Border.png", S(-2), S(-2))

    # 2. Character art: scale original low-res card to canvas, crop to photo window
    photo_src = Image.open(find_photo(ROW)).convert("RGBA")
    photo_full = photo_src.resize((S(BASE_W), S(BASE_H)), Image.LANCZOS)
    photo_window = photo_full.crop((S(32), S(140), S(672), S(574)))
    canvas.alpha_composite(photo_window, dest=(S(32), S(140)))

    # 3. Card Background.
    # Layer_4 is the notched photo-border strip. In the PSD it uses BlendMode.DIFFERENCE,
    # not normal compositing — pasting it as opaque produces solid black teeth instead
    # of the partly-transparent look the original card has. Apply Difference blend
    # (|base - top| per RGB channel, masked by the layer's alpha) instead.
    layer4 = scale_asset(Image.open(ASSETS / "Card_Background/Layer_4.png").convert("RGBA"))
    apply_difference(canvas, layer4, (S(32), S(140)))
    paste_rgba(canvas, ASSETS / "Card_Background/Layer_11.png", S(27), S(26))

    # 4. Staffing / affiliation icons — data-driven from the card's fields.
    if is_ship:
        # Ships: vertical staffing-requirement column (Staff field) plus the
        # affiliation/era icon (Icons field) reusing the slot 2/3 sockets.
        render_ship_staffing(canvas, ROW["Staff"])
        render_icons(canvas, ROW["Icons"])
    else:
        # Personnel: staffing icons come entirely from the Icons field
        # e.g. "[Stf][TNG][Fut]"
        render_icons(canvas, ROW["Icons"])

    # 5. Attribute labels bar background (asset is transparent; labels rendered as text below)

    # 6. Unique dot next to name (only if card is flagged Unique)
    if ROW["Unique"].upper() == "Y":
        paste_rgba(canvas, ASSETS / "Cost__Name__Title__and_Class_Race/Card_Name/Unique/Unique.png", S(196), S(63))

    # -----------------------------------------------------------------------
    # Text overlays
    # Sizes calibrated via getbbox() measurements against spec.json layer bboxes.
    # -----------------------------------------------------------------------

    draw = ImageDraw.Draw(canvas)
    BLACK = (0, 0, 0, 255)
    WHITE = (255, 255, 255, 255)

    # Cost — white, centred in 19x23 circle at (143, 59)
    font_cost = gfont(F_CRILEE, PT_COST)
    cost_text = ROW["Cost"]
    cost_y = vcenter_y(S(59), S(23), font_cost, cost_text)
    cost_x = S(143) + (S(19) - int(draw.textlength(cost_text, font=font_cost))) // 2
    draw.text((cost_x, cost_y), cost_text, font=font_cost, fill=WHITE)

    # Name — black, left-aligned after unique dot (or further left if not unique)
    font_name = gfont(F_CRILEE, PT_NAME)
    name_x = S(213) if ROW["Unique"].upper() == "Y" else S(196)
    name_y = vcenter_y(S(56), S(30), font_name, NAME)
    draw.text((name_x, name_y), NAME, font=font_name, fill=BLACK)

    # Title — vert-centred in 22px bbox at y=93
    font_title = gfont(F_CRILEE, PT_TITLE)
    title_y = vcenter_y(S(93), S(22), font_title, TITLE)
    draw.text((S(210), title_y), TITLE, font=font_title, fill=BLACK)

    # Class/Race oval — centred in [287, 552, 440, 588]. Ships show their Class
    # (e.g. "Defiant Class"); personnel show their Species.
    font_species = gfont(F_FUTURA_BOLD, PT_SPECIES)
    species_text = ROW["Class"] if is_ship else ROW["Species"]
    sp_tw = int(draw.textlength(species_text, font=font_species))
    sp_y = vcenter_y(S(552), S(36), font_species, species_text)
    cx = (S(287) + S(440)) // 2
    draw.text((cx - sp_tw // 2, sp_y), species_text, font=font_species, fill=BLACK)

    # Skills: flow layout — personnel only (ships have no skills line). Wrap at
    # the Skill Text right edge (spec: x=646), spacing from spec measurements.
    # dot_w=21px, dot-to-text gap=4px, inter-skill gap=12px, row spacing=33px
    cur_row = 0
    if not is_ship:
        SKILLS = reflow_skills(ROW["Skills"].split())
        font_skill = gfont(F_FUTURA_BOLD, PT_SKILL)
        skills = SKILLS
        dot_path = ASSETS / "Skills_and_Flavor_Text/Personnel/Skill_1/Dot.png"
        DOT_W, DOT_TEXT_GAP, INTER_GAP = S(21), S(4), S(12)
        SKILL_LEFT, SKILL_RIGHT = S(126), S(646)
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
    TEXT_LEFT, TEXT_RIGHT, TEXT_BOTTOM = 126, 632, 840
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
    # The three label/value x-positions are shared between both layouts.
    if is_ship:
        attr_labels = [("Range", S(126)), ("Weapons", S(335)), ("Shields", S(548))]
        attr_values = [(ROW["Range"], S(237)), (ROW["Weapons"], S(449)), (ROW["Shields"], S(662))]
    else:
        attr_labels = [("Integrity", S(126)), ("Cunning", S(335)), ("Strength", S(548))]
        attr_values = [(ROW["Integrity"], S(237)), (ROW["Cunning"], S(449)), (ROW["Strength"], S(662))]

    font_attr_cap = gfont(F_FUTURI_BOLD, PT_ATTR_CAP)
    font_attr_sc = gfont(F_FUTURI_BOLD, PT_ATTR_SC)
    for label, lx in attr_labels:
        cap, rest = label[0], label[1:].upper()
        # Vertically centre the larger cap in the bar, then align the small-caps bottom to it.
        cap_y = vcenter_y(S(916), S(23), font_attr_cap, cap)
        cap_bottom = cap_y + font_attr_cap.getbbox(cap)[3]
        rest_y = cap_bottom - font_attr_sc.getbbox(rest)[3]
        draw.text((lx, cap_y), cap, font=font_attr_cap, fill=WHITE)
        cap_w = int(draw.textlength(cap, font=font_attr_cap))
        draw.text((lx + cap_w, rest_y), rest, font=font_attr_sc, fill=WHITE)

    # Attribute values — right-aligned to spec right-edges x=237, 449, 662
    font_attr = gfont(F_FUTURI_BOLD, PT_ATTR)
    bar_top, bar_h = S(916), S(23)
    for val, rx in attr_values:
        tw = int(draw.textlength(val, font=font_attr))
        vy = vcenter_y(bar_top, bar_h, font_attr, val)
        draw.text((rx - tw, vy), val, font=font_attr, fill=WHITE)

    # Rarity — centred in [619, 984, 669, 996]
    font_rarity = gfont(F_FUTURA_BOLD, PT_RARITY)
    rarity_text = format_rarity(ROW["CollectorsInfo"])
    tw = int(draw.textlength(rarity_text, font=font_rarity))
    r_y = vcenter_y(S(984), S(12), font_rarity, rarity_text)
    cx = (S(619) + S(669)) // 2
    draw.text((cx - tw // 2, r_y), rarity_text, font=font_rarity, fill=BLACK)

    # Disclaimer — small white text rotated 90° CCW, running up the right edge
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

    if shutil.which("tesseract") is None:
        raise SystemExit("tesseract CLI not found on PATH (needed for NAME/TITLE "
                         "OCR). Install it or pin cards via " + str(NAME_TITLE_OVERRIDE))

    rows = load_rows(input_path)
    overrides = load_name_title_overrides(NAME_TITLE_OVERRIDE)
    outdir = OUTDIR if dpi == BASE_DPI else OUTDIR / f"{dpi}dpi"
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"Rendering {len(rows)} card(s) from {input_path} at {dpi} DPI (scale {SCALE:.3f})")
    rendered = 0
    low_conf = []
    for row in rows:
        cid = row["CollectorsInfo"]
        try:
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
