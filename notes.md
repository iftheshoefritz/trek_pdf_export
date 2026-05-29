# Trek export — working notes

## Project principle (read this first)

This entire project is about **assembling high-DPI card images from low-res
source material**. The two halves of every card are treated very differently:

- **Text and frame — rendered crisply, resolution-independent.** All card text
  (name, cost, game text, skills, attributes, rarity, the "Dilemma"/type label,
  etc.) is drawn from the card *data* using real fonts, and the frame/icons come
  from vector-ish template assets. These render natively at whatever `--dpi` is
  requested, so they stay sharp at any output size. 300-DPI is just the design
  space; 600/800 DPI scale the text and layout up losslessly.

- **Character art — upscaled with low effort, source-limited.** The photo is the
  one piece we don't have at high resolution (the Decipher scans are ~357×499).
  We just LANCZOS-resize it to fill the card and crop the art window. It will
  look soft when rendered large, and that's expected — it's a hard input limit,
  not a rendering bug. Don't spend effort trying to make the art "HD"; a fancier
  upscaler (super-resolution) is the only lever, and it's explicitly optional
  (see "Upscaling the card photo"). The text staying crisp is what carries the
  perceived quality.

So when a render "looks low res," check *which* part: crisp text + frame over a
soft photo is the intended result.

## Alternative icon source

`/Users/frederickmeissner/devprojects/webula/public/icons/` contains 24×24 GIF
icons for staffing (staff, command), eras (tng, ds9, voyager, tos, future,
past, au, maquis, earth), affiliations, and card types. Single-ring icons
(no nested-ring artefacts).

These are lower resolution than the sprite cells in
`templates/2e HD Icon Set v1.psd` (~30×30), so the sprite is the primary
source. Keep webula in mind as a fallback if a sprite cell is missing or
visually wrong.

## Verifying card renders

When changing icon extraction or card layout, always re-run the pipeline and
look at the output PNG. Pixel measurements alone are misleading — e.g. a
sprite cell's alpha bbox is its full extent, but the *visual* ring inside is
much smaller due to anti-aliasing/halo. The dark-ring bbox is the right thing
to measure for size-matching against TNG.

Pipeline:
```
python3 extract_icon_assets.py   # regenerate Staff/Future PNGs from sprite
python3 extract_socket.py        # regenerate Card_Background/Icon_Socket.png
python3 reconstruct_card.py      # render all cards into fixture/reconstructed/
```

`extract_socket.py` only needs to be re-run if Layer_11 is re-extracted or
the socket asset is deleted; otherwise the saved PNG is fine to commit.

## Card_Background chrome-panel rows

Layer_11's chrome panel luminance varies row-to-row: at slot 3's row (canvas
y≈797) it reads ~225, while at slot 1/2's rows it reads ~190. This is why
`extract_socket.py` crops the strip tightly (ending at the right chrome rim)
and tapers the alpha on the right 5 columns — naively copying slot 2's full
chrome strip onto slot 3 would create a visible darker patch. The taper lets
the existing Layer_11 panel show through under each slot's actual row.

## Rendering cards

`reconstruct_card.py` takes a tab-separated card data file (header row + one row
per card, same column layout as `cards_with_processed_columns.txt`) and renders
every row:

```
python3 reconstruct_card.py [INPUT.txt] [--dpi 300|600|800]
```

If `INPUT.txt` is omitted it defaults to
`fixture/federation_personnel_fixture.txt`. Output goes to
`fixture/reconstructed/<CollectorsInfo>.png` (300 DPI) or
`fixture/reconstructed/<dpi>dpi/<CollectorsInfo>.png` for higher DPI. Each card's
photo is looked up as `<CollectorsInfo>.jpg` then `<ImageFile>.jpg` in
`fixture/low quality decipher images/`.

### Output DPI (300 / 600 / 800)

All layout constants are authored in **300-DPI design space** (a 736×1029 card).
`--dpi` sets `SCALE = dpi/300` and every measurement, font size and asset is
scaled by it via the `S()` helper (so 600→1472×2058, 800→1963×2744). Text is
drawn natively at the scaled point size (stays crisp); the raster template
assets and the low-res photo are LANCZOS-upscaled (source-limited — they don't
gain real detail, but text does). Adding a new layout constant? Express it in
300-DPI terms and wrap it in `S()` (coords/sizes), use `gfont()` for fonts, and
`scale_asset()`/`paste_rgba()` for images — never paste a raw asset.

**Why the chrome can't be sharper (don't re-investigate):** every `templates/*.psd`
is exactly 736×1029 px @ 300 DPI ("HD" is just the product name), the extracted
assets already match that full native size, and the chrome layers are plain
raster `PixelLayer`s — no Smart Objects with embedded hi-res art, no vector fills
to re-render. So re-extracting "at higher resolution" yields byte-identical
assets; 300 DPI is the hard ceiling of the source. Runtime LANCZOS upscaling is
the chosen approach (chrome is mostly gradients, which upscale cleanly). Genuine
options if ever needed: super-resolution (Real-ESRGAN) baked per-DPI, vectorizing
key elements, or sourcing genuinely higher-res templates.

### NAME / TITLE split (OCR-inferred)

Column 1 concatenates name + title ("Data Lucasian Chair") with no reliable
split rule. The script infers the split per card, every run:

1. OCR the card scan's top (name) row band (`NAME_ROW_BOX`, the ~357×499
   Decipher layout) with the `tesseract` CLI.
2. The OCR text is usually garbled, but we already know the exact tokens from
   column 1 — so we only need the *boundary*. `infer_name_title()` scores every
   candidate split point by fuzzy-matching `tokens[:k]` against the OCR text
   (`difflib.SequenceMatcher`) and takes the best.
3. Confidence = best fuzzy score and its margin over the runner-up. A split is
   flagged LOW CONFIDENCE (printed at the end) unless `score >= 0.85` or
   `margin >= 0.15`. Short single-word names score low in absolute terms but win
   by a wide margin, hence the OR.

Validated 9/9 on the fixture (scores 0.73–1.00), including garbled OCR
(`fasha`→Tasha, `Wort`→Worf) and multi-word names (William T. Riker, Geordi La
Forge). Cost: ~0.08s/card.

Requires `tesseract` on PATH (the script errors early if missing). To override a
wrong split, add a row to `fixture/name_title_map.tsv`
(`CollectorsInfo<TAB>Name<TAB>Title`); it's empty by default and takes
precedence over OCR when present.

## Scope

The script handles **Federation Personnel and Ship** cards; it branches on the
row's `Type` column. Ships use a vertical staffing column (from the `Staff`
field, mapped top-to-bottom onto slots 1–5 via `render_ship_staffing()`), the
`Class` oval instead of `Species`, the Range/Weapons/Shields attribute bar, and
no skills row — keyword + game text fill the whole text band. The affiliation/era
icon (ship `Icons` field) reuses the personnel slot 2/3 sockets via
`render_icons()`. Ship staffing/icon/attribute assets live under
`extracted/federation/assets/Staffing_and_Attributes/Ship/`.

**Dilemma** cards now have a rendering path (`render_dilemma()`), with assets
extracted by `extract_dilemma_assets.py` into `extracted/dilemma/`. Event,
Mission and other card types still need their own paths. Other affiliations
(Klingon/Romulan/etc.) have their own PSD templates under `templates/` and their
own extracted asset trees.

Staffing icons sit *inside* the photo window (unlike personnel, whose staffing
is on the chrome below the photo). The template only carries populated per-slot
icons for the slots that were filled on the source PSD's example ship; empty
slots would let the low-res scan's own printed icons bleed through (blurry, no
socket ring, misaligned). So `render_ship_staffing()` uses one canonical crisp
icon per kind (`SHIP_STAFF_ICON`) for every slot.

The icon keeps its dark socket disc — this is *authentic*, not an artifact: the
printed card has a dark button behind each staffing star (clearly visible on the
silver staff stars over Voyager's bright hull in the source scan). The disc also
occludes the low-res scan's own printed star beneath the icon. Keying the disc
to transparent was tried and rejected: it let the scan's star ghost through,
slightly offset from our crisp star (a visible double-icon).

Known limitation (shared with personnel): the NAME/TITLE OCR split assumes a
title exists whenever column 1 has 2+ words, so title-less ship names like
"U.S.S. Akira" and "Maquis Raider" mis-split. These are pinned in
`fixture/name_title_map.tsv` (NAME = full name, TITLE column omitted/empty); the
override loader now accepts a 2-field row for title-less names.

The keywords renderer currently assumes the keywords line fits on one row;
multi-line keywords are not yet handled. It also splits only on the first ": ",
so a field with several keyword statements (e.g. Riker's "Admiral. Commander:
U.S.S. Enterprise-D.") is not styled per-statement.

## Lower text block (lore + game text)

`draw_textflow()` renders the lore/keyword line and the game text as a rich
flow that supports:
  - bold/medium/italic runs — a leading "Order -"-style lexeme in game text is
    bold (`gametext_runs`); the keyword type before ": " is bold and the value
    after is italic (`keyword_runs`);
  - inline icons — `[TNG]` etc. in the text render as a small icon centred on
    the line's midline, from `extracted/icons/inline/<stem>.png` (copied from
    the webula 24×24 single-ring icons; see `INLINE_ICON_MAP`);
  - left-aligned, ragged right (the printed cards are not justified);
  - uniform line height (always room for an icon, present or not);
  - auto-shrink: game text starts at the nominal size and shrinks to fit the
    band down to spec y=840 rather than truncating — Decipher cards likewise
    shrink dense game text. The skill flow wraps at the Skill Text right edge
    (spec x=646), wider than the game text box (x=632).

Keyword + game-text layout: the game text continues inline on the keyword's line
(one combined flow) when there is room, e.g. "**Cloaking Device.** While this
ship...". The exception is an "Order -"-style game text (a bold leading lexeme,
matched by `^[A-Z][A-Za-z]+ -`): it starts on its own line, so the keyword and
game text are drawn as two separate blocks. Applies to both ships and personnel.

## Rendering the full fixture (federation_personnel_fixture.txt)

The fixture holds 9 Federation Personnel cards. `reconstruct_card.py` can do any
one of them today, but rendering all of them needs the following:

1. **Photos — COMPLETE.** All 9 are now in `fixture/low quality decipher images/`,
   copied from `/Users/frederickmeissner/devprojects/webula/public/cardimages/`
   (keyed by ImageFile). See the DPI caveat under "Upscaling the card photo".

2. **Personnel staffing icons — COMPLETE.** All staffing-slot icons now resolve:
   - Slot 1: Command (sprite r4c0), Staff (r4c1)
   - Slot 2: TNG, DS9, Voyager, TOS, Maquis, Earth — baked 47×47 versions from
     the Ship-group composite (ring included), all real and correct
   - Slot 3: AU (sprite r2c0), Past (r2c1), Future (r2c2) — the time-period
     swirl triplet on sprite row 3 (AU=gold, Past=red, Future=blue)
   `extract_icon_assets.py` emits the Slot_1/Slot_3 ones from the sprite.
   Audit rule for spotting broken icons: a Slot_1/Slot_3 icon PNG that is large
   AND fully opaque is a background slice, not a real icon. (Slot_2 era icons
   are legitimately 47×47 fully opaque because their ring is baked into
   Layer_11.)

   Not yet extracted (not needed for Personnel staffing slots, but present in
   `templates/2e HD Icon Set v1.psd` for future card types): affiliation/species
   emblems (Klingon, Romulan, Cardassian, Dominion, Bajoran, Federation, …) on
   sprite rows 1–2, and the quadrant letters Alpha (r3c2), Gamma (r3c0),
   Delta (r3c1). These belong to other parts of the card (not the staffing
   column), so extract them into a general icon library when those card types
   are tackled. Auto-matching sprite cells against the webula GIFs is unreliable
   for the affiliation cells (whole-cell RGB is dominated by the ring) — verify
   visually.

3. **NAME / TITLE split per card — COMPLETE.** Inferred per card by OCR of the
   name row + fuzzy alignment against the known tokens (see "NAME / TITLE split
   (OCR-inferred)" above). `fixture/name_title_map.tsv` is now an optional
   override, empty by default.

4. **Multi-keyword lines.** William T. Riker's Keywords field is
   "Admiral. Commander: U.S.S. Enterprise-D." — two keywords in one field. The
   current renderer splits on the first ": " only, so it would set both
   "Admiral." and "Commander:" upright. Correct styling needs to handle a list
   of keyword statements, each either a standalone keyword or "Type: value",
   with the right upright/italic treatment. Verify against an original.

5. **Batch driver — COMPLETE.** `reconstruct_card.py [INPUT.txt]` loops over all
   rows in the input file (default `fixture/federation_personnel_fixture.txt`)
   and renders each into `fixture/reconstructed/<CollectorsInfo>.png`.

## Upscaling the card photo (to be scripted)

**Target print spec: a standard TCG card (2.5" × 3.5") at a DPI that varies by
print service — at least 300 DPI, possibly as high as 800 DPI.** So the target
pixel size is a range, not fixed: 750×1050 at 300 DPI up to 2000×2800 at 800
DPI. The canvas here is 736×1029 (≈300 DPI). Design the pipeline to hit the
high end, since downscaling to a lower-DPI printer is lossless but upscaling is
not.

The source Decipher scans fall far short of even the floor: they are ~357×497
px, which on a 2.5"×3.5" card is only **~143 DPI** (357/2.5, 497/3.5) — under
half the 300 DPI minimum, and ~1/5 of the 800 DPI ceiling. This is the core
problem the upscaling step exists to solve.

The "image portion" is the character art inside the photo window
(canvas crop `(32, 140, 672, 574)`, i.e. 640×434 px at 300 DPI).

Current process (in `reconstruct_card.py`): take the low-res Decipher scan
(~357×499), LANCZOS-resize the *whole* image to the 736×1029 canvas, then crop
the photo window. This ~2× upscale is the current source of truth and the
results have been good enough to ship.

Future work (optional quality improvement): the LANCZOS step could be replaced
with a dedicated super-resolution pass on the cropped photo region before
compositing. Possible avenues:
  - Real-ESRGAN (general / anime-tuned models)
  - waifu2x (good on illustration/clean edges)
  - Topaz Gigapixel AI (commercial, strong on photos)
  - GFPGAN / CodeFormer specifically for the face regions (these are portraits)
  - a two-pass approach: SR upscale, then mild sharpen/denoise
If pursued, factor it into an `upscale_photo.py` step that emits a high-res
photo window for `reconstruct_card.py` to consume instead of resizing inline.

## Flavor text

Not currently rendered. `cards_with_processed_columns.txt` has 28 columns and
none of them is flavor text. If/when a flavor source is added, plumb it into
`reconstruct_card.py` below the game text block.

## Bold game-text detection

**DILEMMAS ONLY.** Bold game text is a dilemma-specific convention — it marks
the "requirements" to overcome a dilemma. No other card type uses bold this way,
so `detect_bold_gametext.py` is meant to be run only on dilemma rows; running it
on other types is meaningless.

Bold game text marks 2E "requirements" (skills, attributes, cost, number of
personnel, ...) and matters for gameplay, but no card-data source records it.
`detect_bold_gametext.py` recovers it from the scans and writes
`fixture/gametext_bold.tsv`, which `reconstruct_card.py` reads (via the
`GAMETEXT_MARKUP` sidecar) to render `<b>..</b>` runs. See the script's module
docstring for the full pipeline and rules. Two design facts worth keeping front
of mind:

- **Per-card self-calibration is mandatory.** The dilemmas span 10+ years and
  are mostly *virtual* (never-printed) cards with no common print/scan/render
  process, so there is no absolute or cross-card weight scale — bold vs regular
  is only separable *within* one card. Hence per-card / per-line baselines, and
  detection per word-instance (the same word can be bold in one place and not
  another, so we never key off word identity).
- **Recall-biased on purpose.** A missed bold is a silent gameplay error; an
  extra one is obvious in review and easy to trim. Thresholds favour
  over-flagging; the sidecar is a reviewed first pass.

Cases the pixels can't resolve (heavy-print or low-contrast cards) are pinned in
`fixture/gametext_bold_overrides.tsv` and merged at write time, so they survive
re-running the detector. `--review-dir DIR` writes per-card crops with the
detected-bold words boxed, for eyeballing against the scans.

## Reference deck and source PDFs

`reference_export/reference_deck_input.txt` is the canonical test deck driving
the pipeline (lines are `<collector-code>\t<name>`). The per-type fixtures
`fixture/deck_*.txt` are derived from it: normalise each collector code
(`<set><rarity><number>` with the number zero-padded to 3 digits, e.g.
`0VP19` → `0VP019`) to match `CollectorsInfo` in
`cards_with_processed_columns.txt`, then group the matched rows by `Type`.

The source PDFs are large (~148MB total) and are **git-ignored** (`fixture/*.pdf`,
plus the `fixture/*_cards/` images sliced from them) — kept locally only.

Source PDFs under `fixture/`:
- `2eed_hires.pdf` — the 2E errata document: more recent updates to older cards,
  so it spans many eras. Its text is *real text in the actual fonts*
  (regular = `Futura-Condensed`, bold = `Futura-CondensedBold`), so bold would be
  recoverable directly from font names — but **it covers very few of the cards we
  care about**, so it can never be more than a patch over a small handful.
  **OUT OF SCOPE — do not propose this as a bold-detection improvement.** It is
  deliberately set aside until the scan-based approaches in
  `detect_bold_gametext.py` are exhausted (the in-script borderline-disambiguation
  priors, per-line calibration tuning, etc.). Revisit only if those are genuinely
  played out; until then it is not a replacement for, nor a layer above,
  `detect_bold_gametext.py`.
- `ReturntoGrace_hires.pdf`, `2e_eratta_sample.pdf` — further PDFs, not yet
  examined. `extract_cards.py` slices individual card images out of such grids.
