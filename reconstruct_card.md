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
