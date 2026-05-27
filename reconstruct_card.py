#!/usr/bin/env python3
"""
Reconstruct Data Lucasian Chair (WYLB078) using the extracted Federation PSD
template assets and the original low-quality card image as the character art.

Outputs: fixture/reconstructed_WYLB078.png
"""

from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

CANVAS_W, CANVAS_H = 736, 1029
ASSETS = Path("extracted/federation/assets")
FONTS  = Path("fonts")
OUT    = Path("fixture/reconstructed_WYLB078.png")

# Actual card fonts from fonts/
F_CRILEE        = FONTS / "Crillee Italic BT.ttf"
F_FUTURA_BOLD   = FONTS / "Futura LT Condensed Bold.ttf"
F_FUTURA_BOLDO  = FONTS / "Futura LT Condensed Bold Oblique.ttf"
F_FUTURA_MED    = FONTS / "Futura LT Condensed Medium.ttf"
F_FUTURI_BOLD   = FONTS / "FUTURCB.TTF"    # FuturiCondensedBoldSWFTE


def load_font(path, size):
    try:
        return ImageFont.truetype(str(path), size)
    except Exception:
        return ImageFont.load_default()


def paste_rgba(canvas, asset_path, x, y):
    src = Image.open(asset_path).convert("RGBA")
    canvas.alpha_composite(src, dest=(x, y))


def vcenter_y(bbox_top, bbox_h, font, text):
    """Return y so text is vertically centred within the bbox."""
    fb = font.getbbox(text)
    return bbox_top + (bbox_h - (fb[3] - fb[1])) // 2 - fb[1]


def draw_text_wrapped(draw, text, bbox, font, fill, leading=None):
    l, t, r, b = bbox
    max_w = r - l
    words = text.split()
    lines, line = [], []
    for word in words:
        test = " ".join(line + [word])
        if draw.textlength(test, font=font) <= max_w:
            line.append(word)
        else:
            if line:
                lines.append(" ".join(line))
            line = [word]
    if line:
        lines.append(" ".join(line))
    if leading is None:
        asc, desc = font.getmetrics()
        leading = asc + desc
    y = t
    for ln in lines:
        if y + leading > b:
            break
        draw.text((l, y), ln, font=font, fill=fill)
        y += leading


# ---------------------------------------------------------------------------
# Build canvas — spec.json order is bottom-to-top (index 0 = bottommost)
# ---------------------------------------------------------------------------

canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), (0, 0, 0, 0))

# 1. Black Border — background, provides outer card colour
paste_rgba(canvas, ASSETS / "Black_Border.png", -2, -2)

# 2. Character art: scale original low-res card to canvas, crop to photo window
photo_src = Image.open("fixture/low quality decipher images/WYLB078.jpg").convert("RGBA")
photo_full = photo_src.resize((CANVAS_W, CANVAS_H), Image.LANCZOS)
photo_window = photo_full.crop((32, 140, 672, 574))
canvas.alpha_composite(photo_window, dest=(32, 140))

# 3. Card Background.
# Layer_4 is the notched photo-border strip. In the PSD it uses BlendMode.DIFFERENCE,
# not normal compositing — pasting it as opaque produces solid black teeth instead
# of the partly-transparent look the original card has. Apply Difference blend
# (|base - top| per RGB channel, masked by the layer's alpha) instead.
def apply_difference(base, top, dest):
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

apply_difference(canvas, Image.open(ASSETS / "Card_Background/Layer_4.png"), (32, 140))
paste_rgba(canvas, ASSETS / "Card_Background/Layer_11.png", 27, 26)

# 3b. Icon sockets for slots 1 & 3 — replicate the chrome socket+tab that
#     Layer_11 has baked in around slot 2. Socket centre in the strip is (30, 30);
#     paste so it lands on each slot's ring centre.
SOCKET_ASSET = ASSETS / "Card_Background/Icon_Socket.png"
SOCKET_CENTRE = (30, 30)
SLOT_RING_CENTRES_FOR_SOCKET = [(72, 653), (72, 797)]  # slot 1 & slot 3
for (cx, cy) in SLOT_RING_CENTRES_FOR_SOCKET:
    paste_rgba(canvas, SOCKET_ASSET, cx - SOCKET_CENTRE[0], cy - SOCKET_CENTRE[1])

# 4. Personnel staffing icons — data-driven from card Icons field e.g. "[Stf][TNG][Fut]"
import re as _re

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
import json as _json

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
    for abbrev in _re.findall(r'\[([^\]]+)\]', icons_str):
        entry = ICON_MAP.get(abbrev)
        if not entry:
            print(f"  ! unknown icon: [{abbrev}]")
            continue
        slot, rel = entry
        asset_path = ASSETS / "Staffing_and_Attributes" / rel
        cx, cy = SLOT_RING_CENTRE[slot]
        # Use ring-centre metadata if present, else fall back to image centre
        meta_path = asset_path.with_suffix(".json")
        src = Image.open(asset_path).convert("RGBA")
        if meta_path.exists():
            m = _json.loads(meta_path.read_text())
            rcx, rcy = m["ring_cx"], m["ring_cy"]
        else:
            rcx, rcy = src.width // 2, src.height // 2
        canvas.alpha_composite(src, dest=(cx - rcx, cy - rcy))

render_icons(canvas, "[Stf][TNG][Fut]")

# 5. Attribute labels bar background (asset is transparent; labels rendered as text below)

# 6. Unique dot next to name
paste_rgba(canvas, ASSETS / "Cost__Name__Title__and_Class_Race/Card_Name/Unique/Unique.png", 196, 63)

# ---------------------------------------------------------------------------
# Text overlays
# Sizes calibrated via getbbox() measurements against spec.json layer bboxes.
# ---------------------------------------------------------------------------

draw = ImageDraw.Draw(canvas)
BLACK = (0, 0, 0, 255)
WHITE = (255, 255, 255, 255)

# Sizes derived from PDF: PDF_pts × (300 DPI / 72 pts/inch) = PIL px
# Confirmed against Benjamin Sisko card in fixture/2eed_hires.pdf
font_cost    = load_font(F_CRILEE,       33)   # Crillee 8.0pt × 4.167 = 33px
font_name    = load_font(F_CRILEE,       35)   # Crillee 8.5pt × 4.167 = 35px
font_title   = load_font(F_CRILEE,       27)   # Crillee 6.5pt × 4.167 = 27px
font_species = load_font(F_FUTURA_BOLD,  33)   # Futura-CondBold 8.0pt = 33px
font_skill   = load_font(F_FUTURA_BOLD,  29)   # Futura-CondBold 7.0pt = 29px
font_game    = load_font(F_FUTURA_MED,   29)   # Futura-Cond Medium 7.0pt = 29px
font_attr    = load_font(F_FUTURI_BOLD,  28)   # Futura-CondBold-SC700 6.75pt = 28px
font_rarity  = load_font(F_FUTURA_BOLD,  17)   # Futura-CondBold 4.0pt = 17px

# Cost "5" — white, centred in 19x23 circle at (143, 59)
cost_y = vcenter_y(59, 23, font_cost, "5")
cost_x = 143 + (19 - int(draw.textlength("5", font=font_cost))) // 2
draw.text((cost_x, cost_y), "5", font=font_cost, fill=WHITE)

# Name "Data" — black, left-aligned after unique dot; vert-centred in bbox h=30
name_y = vcenter_y(56, 30, font_name, "Data")
draw.text((213, name_y), "Data", font=font_name, fill=BLACK)

# Title "Lucasian Chair" — vert-centred in 22px bbox at y=93
title_y = vcenter_y(93, 22, font_title, "Lucasian Chair")
draw.text((210, title_y), "Lucasian Chair", font=font_title, fill=BLACK)

# Species oval — centred in [287, 552, 440, 588]
species_text = "Android"
sp_tw = int(draw.textlength(species_text, font=font_species))
sp_y = vcenter_y(552, 36, font_species, species_text)
cx = (287 + 440) // 2
draw.text((cx - sp_tw // 2, sp_y), species_text, font=font_species, fill=BLACK)

# Skills: flow layout — wrap at x=632, spacing from spec measurements
# dot_w=21px, dot-to-text gap=4px, inter-skill gap=12px, row spacing=33px
skills = ["2 Astrometrics", "Engineer", "Exobiology", "Physics", "Programming", "2 Science"]
dot_path = ASSETS / "Skills_and_Flavor_Text/Personnel/Skill_1/Dot.png"
DOT_W, DOT_TEXT_GAP, INTER_GAP = 21, 4, 12
SKILL_LEFT, SKILL_RIGHT = 126, 632
ROW0_DOT_Y, ROW0_TEXT_Y, ROW_SPACING = 646, 644, 33

DOT_H = 21
x, cur_row = SKILL_LEFT, 0
for skill in skills:
    tw = int(draw.textlength(skill, font=font_skill))
    slot_w = DOT_W + DOT_TEXT_GAP + tw + INTER_GAP
    if x > SKILL_LEFT and x + slot_w - INTER_GAP > SKILL_RIGHT:
        cur_row += 1
        x = SKILL_LEFT
    text_y = ROW0_TEXT_Y + cur_row * ROW_SPACING
    sk_y = vcenter_y(text_y, 23, font_skill, skill)
    # Centre dot on actual rendered text bounds
    fb = font_skill.getbbox(skill)
    text_mid = (sk_y + fb[1] + sk_y + fb[3]) / 2
    dot_y = int(text_mid - DOT_H / 2)
    paste_rgba(canvas, dot_path, x, dot_y)
    draw.text((x + DOT_W + DOT_TEXT_GAP, sk_y), skill, font=font_skill, fill=BLACK)
    x += slot_w

# Game text — positioned below last skill row
game_y = ROW0_TEXT_Y + cur_row * ROW_SPACING + 35
game_text = ("When you play this personnel, if you have completed a mission "
             "requiring Diplomacy or Leadership, he is cost -4.")
draw_text_wrapped(draw, game_text, [126, game_y, 632, game_y + 120], font_game, BLACK, leading=31.25)

# Attribute labels — small-caps: first letter at 28px, remainder uppercase at 21px
# x-positions mirror Ship group Range/Weapons/Shields labels from spec
font_attr_cap  = load_font(F_FUTURI_BOLD, 28)   # large initial cap
font_attr_sc   = load_font(F_FUTURI_BOLD, 21)   # small-caps body
for label, lx in [("Integrity", 126), ("Cunning", 335), ("Strength", 548)]:
    cap, rest = label[0], label[1:].upper()
    # Vertically centre the larger cap in the bar, then align the small-caps bottom to it.
    cap_y = vcenter_y(916, 23, font_attr_cap, cap)
    cap_bottom = cap_y + font_attr_cap.getbbox(cap)[3]
    rest_y = cap_bottom - font_attr_sc.getbbox(rest)[3]
    draw.text((lx, cap_y), cap, font=font_attr_cap, fill=WHITE)
    cap_w = int(draw.textlength(cap, font=font_attr_cap))
    draw.text((lx + cap_w, rest_y), rest, font=font_attr_sc, fill=WHITE)

# Attribute values — right-aligned to spec right-edges x=237, 449, 662
bar_top, bar_h = 916, 23
for val, rx in [("6", 237), ("10", 449), ("10", 662)]:
    tw = int(draw.textlength(val, font=font_attr))
    vy = vcenter_y(bar_top, bar_h, font_attr, val)
    draw.text((rx - tw, vy), val, font=font_attr, fill=WHITE)

# Rarity — centred in [619, 984, 669, 996]
rarity_text = "14 R 78"
tw = int(draw.textlength(rarity_text, font=font_rarity))
r_y = vcenter_y(984, 12, font_rarity, rarity_text)
cx = (619 + 669) // 2
draw.text((cx - tw // 2, r_y), rarity_text, font=font_rarity, fill=BLACK)

# Disclaimer — small white text rotated 90° CCW, running up the right edge
disclaimer = "NOT ENDORSED BY CBS OR PAR. PIC."
font_disclaimer = load_font(F_FUTURA_BOLD, 12)
tw = int(font_disclaimer.getlength(disclaimer))
asc, desc = font_disclaimer.getmetrics()
th = asc + desc
strip = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
# Mid-grey + reduced alpha so the text reads as a faint stencil
ImageDraw.Draw(strip).text((0, 0), disclaimer, font=font_disclaimer, fill=(170, 170, 170, 130))
strip_rot = strip.rotate(90, expand=True)
# Centre horizontally in the grey strip between the text-box right edge (~x=686)
# and the black border (~x=702): centre x = 694.
GREY_STRIP_CENTRE_X = 694
disclaimer_x = GREY_STRIP_CENTRE_X - strip_rot.width // 2
disclaimer_y = 900 - strip_rot.height
canvas.alpha_composite(strip_rot, dest=(disclaimer_x, disclaimer_y))

# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------

OUT.parent.mkdir(parents=True, exist_ok=True)
canvas.save(OUT, dpi=(300, 300))
print(f"Saved: {OUT}  ({canvas.width}x{canvas.height})")
