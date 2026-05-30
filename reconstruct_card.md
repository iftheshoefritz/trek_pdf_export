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
