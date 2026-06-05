# Reconstruct script — card layout rules

Notes on rules that span card types, captured here (rather than buried in
`reconstruct_card.py` comments) so they're easy to find when adding the
remaining renderers.

## Card name

The card name (column 1 of the data) renders in the Crillee italic font in a
horizontal bar at the top right of the card, to the right of the cost circle.
A single helper, `draw_card_name(...)`, handles every card type.

- **Wrapping.** Long names wrap to a second line at the normal size first; only
  if a 2-line wrap still doesn't fit horizontally does the font shrink (down
  to a 22-pt floor) before re-wrapping.
- **Multi-line anchor.** The LAST wrapped line sits at the single-line vertical
  centre of the name bar; earlier lines stack *upward* into the empty frame
  area above. Wrapping must never grow downward into the photo window.
  (Reference: `How Would You Like a Trip to Romulus?` — the printed card puts
  "to Romulus?" on the bar's normal line, "How Would You Like a Trip" above.)

## Unique dot

Cards flagged `Unique=Y` in the data show a small dot just before the name on
some card types but not others. The dot is the federation template's
`Card_Name/Unique/Unique.png` asset; when present, the name shifts right by
~17 px (design space) to make room.

| Card type  | Show unique dot? |
|------------|------------------|
| Personnel  | yes              |
| Ship       | yes              |
| Event      | yes              |
| Equipment  | yes              |
| Interrupt  | no               |
| Dilemma    | no               |

For interrupts and dilemmas, call `draw_card_name(..., unique=False, ...)`
regardless of the data field — the printed cards do not carry the dot.

## Cost symbol

Most card types carry a cost in the black circle at the top-left of the
name bar (rendered by `draw_cost()`). Interrupts do **not** — the printed
card has no cost circle, and the data's `Cost` column is meaningless on
interrupt rows. Skip `draw_cost` in `render_interrupt`.

| Card type  | Cost symbol? |
|------------|--------------|
| Personnel  | yes          |
| Ship       | yes          |
| Event      | yes          |
| Equipment  | yes          |
| Dilemma    | yes          |
| Interrupt  | no           |

## Title

Personnel and ships carry a *title* (subtitle below the name; e.g.
`Lucasian Chair` for Data). Other card types do not. Title rendering stays in
the personnel/ship renderer.

## Lore / keyword reminder text

Two pieces of printed text are NOT in the card data and so are omitted on
purpose:

- Italic *lore quote* at the bottom of dilemmas and events.
- Italic *reminder text* in parentheses after keywords like `Recall: 1.`
  (e.g. `(While this event is in your discard pile, you may play it…)`).

If a sidecar of these is ever added, gametext rendering would extend to honour
it; for now the omission is the documented gap.

## Ship photo zone & notch strip (non-Federation)

Settled 2026-06-03. The cardbg chrome layers carry decorative chrome that
extends *into* the photo window — the affiliation curls at top-left
(canvas y=140..152) and the class/race band at the bottom (y=552..588 across
the full photo width). Naively zeroing the chrome alpha across the whole
photo window (x=32..672, y=140..574) erases both of those, producing two
visible artifacts that took several rounds to track down.

**The rules `render_card` follows:**

- **Chrome alpha-zero rectangle:** `x=32..672, y=140..552` — stops at the top
  of the class/race band, not at the bottom of the photo window. Keeps the
  entire class band fully opaque across the card so the Class text reads
  cleanly. (Previously zeroing down to y=574 punched a hole through the band
  and produced a horizontal seam through every ship's Class letters at the
  y=574 transition.)
- **Ship-only re-zero:** `x=32..49, y=153..620`. The staffing column from
  just below the affiliation curls down to where the per-slot socket discs
  begin. y=140..152 stays intact so the curls (opaque in Card_Border /
  Layer_12 / Layer_13) remain visible.
- **No photo-pixel zeroing.** The notch difference-blend runs against the
  photo across the strip's full length, which matches the scan — the comb
  teeth show whatever colour the photo has behind them. Earlier versions
  zeroed the photo at the staffing strip to hide scan disc ghosts; that
  produced a visible "top 2/3 opaque, bottom 1/3 photo" seam halfway down
  the notch and was removed.

**Notch (Layer_4 / Image_Frame) per-affiliation config:**

| Affiliation | notch asset                          | cutoff col | alpha |
|-------------|--------------------------------------|------------|-------|
| Federation  | `Card_Background/Layer_4.png`        | 8          | 1.0   |
| Romulan     | `Card_Background/Layer_4.png`        | 15         | 1.0   |
| Non-Aligned | `Card_Background/Layer_4.png`        | 15         | 0.3   |
| Klingon     | `Card_Background/Image_Frame.png`    | 15         | 0.3   |

- **Cutoff** = first column whose alpha is zeroed before the difference blend.
  Federation has a narrow solid zone (cols 0–7) with sparse tips out to 14;
  the other three have a wider solid zone (cols 0–14).
- **Alpha** scales the notch layer's alpha before the blend. Klingon and
  Non-Aligned photo zones are often darker than the notch RGB (~41), and at
  full alpha the difference blend over-brightens those regions. 0.3 keeps the
  effect at the scan's subtle level.
- **Klingon `Image_Frame.png`** has the same alpha channel as Romulan/NA
  Layer_4 — it's routed through the `photo_notch` difference-blend path, not
  pasted directly.

**`SHIP_STAFF_SLOT_XY`:** x=49 (one pixel clear of the notch's right edge at
x=46). All five slots: `(49, 168), (49, 223), (49, 279), (49, 334), (49, 389)`.

## Ship Class oval italicisation

The Class field reads like `K'Vort Class`, `Defiant Class`, `Scimitar Class`.
On the printed card the **first part is italic** (the actual class name) and
the trailing `Class` word is regular weight. `render_card` splits on the
trailing ` Class` suffix and draws the class name in `F_FUTURA_BOLDO`
(italic) and ` Class` in `F_FUTURA_BOLD` (upright), then centres the pair
in the oval.

Edge case: `Flaxian Scout Vessel` and similar entries don't end in `Class`
— the whole field stays upright. Detection is a case-sensitive
`endswith(" Class")` check.

## Card name with no title

Some cards (e.g. `I.K.S. Lukara`, `Flaxian Scout Vessel`, `Bajoran
Interceptor`) have an empty `Title` field — the entire name occupies the name
bar and the subtitle bar is left blank. `draw_card_name` already handles this;
just make sure not to introduce a forced split or fall-back title when none is
in the data.

For multi-word names whose Name/Title split isn't in the data,
`infer_name_title` OCRs the name band and scores every split point. The
candidate range must include `len(tokens)` (i.e. all tokens in the name, empty
title), not stop at `len(tokens) - 1` — otherwise the OCR can never recover a
no-title card like `Bajoran Interceptor` and will always assign the last word
to the title. The full-name candidate wins by score whenever the band reads
both words clearly.

## Borg renderer scope

Sets 1–14 Borg data (audited 2026-06-03):

- **Personnel** (50 cards): Icons field is always exactly `[Cmd]` or `[Stf]`.
  No era / affiliation overlays, no stacking. The Borg PSD's Slot 1 has no
  `Base` layer because none is needed — bake_sockets.py reports `NONE` for
  Borg and that's expected.
- **Ships**: Icons field always empty. Staff field is always a run of 4 or 5
  `[Stf]` only.

The Borg renderer therefore doesn't need per-slot socket discs, era-icon
positioning, or any of the icon-stacking logic Federation needs. Paste Cmd
or Stf directly at the slot 1 position (personnel) or down the staffing
column (ships). Same simplification likely applies to whichever other
affiliations the data shows as single-icon-only — re-audit per affiliation
before assuming.

**Borg AFFIL_CFG specifics** (wired in `reconstruct_card.py`):

- `cardbg_layer="Card_Background/Layer_13.png"` at paste (28,26). The Borg
  PSD's `Card_Background` group contains Layer_4 (photo notch) + Layer_12
  + Layer_13. Layer_13 alone carries the visible chrome; Layer_12 is a
  duplicate copper/bar overlay that, when stacked, produces a visible
  *second* attribute strip below the real one. Don't paste both.
  (`render_card` does support a new `cardbg_layers` list form for affils
  that genuinely need stacked chrome, but Borg doesn't.)
- `photo_notch_cutoff=15` (solid-zone cols 0-14, sparse 15+), same as
  Romulan/Klingon/NA. `photo_notch_alpha` left at 1.0 default — not yet
  tuned against scans; may need scaling down like Klingon's 0.3 if the
  notch over-brightens dark Borg photo zones.
- `per_slot_sockets={}` and `socket_asset=None`: no socket discs at all.
- `ring_centre_offsets={"slot1": (+5,+3)}`: Borg's personnel Slot 1 PSD
  bbox is (48,622,110,689) vs Fed's (42,618,...), so the Cmd/Stf glyph
  shifts right 5 and down 3 from the shared `SLOT_RING_CENTRE` default.
- `ship_staff_slot_xy` (new cfg field): per-affil override of the global
  `SHIP_STAFF_SLOT_XY`. Borg: `[(53,171),(53,227),(53,283),(53,339),(53,395)]`
  (PSD bboxes show +4 right, +3-6 down vs Federation's column).

**Borg-chrome quirk — attribute labels baked into Layer_13.** Unlike
Federation (whose Layer_11 chrome contains *only* the bars), Borg's
Layer_13 has the personnel attribute labels INTEGRITY / CUNNING / STRENGTH
baked into the bottom strip. The renderer draws the row-appropriate labels
as text overlay, so on personnel the baked text ghosted under the rendered
text (visibly doubled) and on ships the baked labels said the wrong thing
(values were right, labels lied). The Borg PSD's intended fix is the Ship
`No_Range.png` / `No_Weapons.png` / `No_Shields.png` overlays, but they
extract as zero-alpha (PSD layer-effect layers — same problem as the slot
bases). Rather than route this through Photopea, `bake_borg_chrome.py`
paints the bright glyph pixels out of Layer_13's attribute band by
per-column median replacement and writes `Layer_13_no_labels.png`. The
Borg AFFIL_CFG points `cardbg_layer` at the cleaned asset. Re-run the
bake if Layer_13.png is ever re-extracted from the PSD.
