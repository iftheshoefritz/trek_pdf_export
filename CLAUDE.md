# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project does

Assembles high-DPI Star Trek CCG 2E card images by combining crisp template-rendered
text/frame layers with low-resolution Decipher card scans for the character art.
The text and frame are resolution-independent (drawn from card data using real
fonts and template assets); only the photo is source-limited (~357×499 scans).
A render that looks soft on the photo but sharp on the text is intended — see
the principle in `notes.md` ("Project principle").

## Pipeline / common commands

```bash
# Regenerate template-derived assets (only when sprite/socket changes)
python3 extract_icon_assets.py
python3 extract_socket.py

# Bake non-Federation affiliation sockets (via headless Photopea — see below)
python3 bake_sockets.py

# Regenerate dilemma/event assets from their PSDs
python3 extract_dilemma_assets.py

# Re-derive a faction's spec.json + asset PNGs from its PSD template
python3 extract_layout_from_template.py "templates/2e HD Federation v1.psd" ./extracted/federation

# Render all rows of a card data file -> fixture/reconstructed/<CollectorsInfo>.png
python3 reconstruct_card.py [INPUT.txt] [--dpi 300|600|800]
# default INPUT is fixture/federation_personnel_fixture.txt

# Detect bold runs in DILEMMA game text from scans -> fixture/gametext_bold.tsv
python3 detect_bold_gametext.py [--review-dir DIR]
```

There is no test suite, lint config, or package manifest. Required CLI tooling:
`tesseract` on PATH (used by `reconstruct_card.py` for NAME/TITLE split and by
`detect_bold_gametext.py`). Python deps used directly: `PIL`/`Pillow`, `psd-tools`,
`numpy`, `scipy` (for `detect_bold_gametext.py`).

## Architecture

Two distinct flows, both rooted at the 736×1029 PSD canvas (300 DPI design space):

1. **PSD → asset library (one-shot, per faction/type).**
   `extract_layout_from_template.py` walks a `templates/*.psd` and writes
   `extracted/<faction-or-type>/`:
   - `spec.json` — full layer tree (bbox, text props for text layers, asset_file
     for pixel layers). Schema documented in `extract_layout_from_template.md`.
   - `assets/...` — one PNG per pixel layer, mirroring the PSD layer hierarchy.
   - `preview.png` — template render with Intro hidden.
   `extract_icon_assets.py`, `extract_socket.py`, `extract_dilemma_assets.py`
   are specialised follow-ups that bake additional crisp PNGs from the sprite
   sheet / chrome layers used by the renderer. `extract_cards.py` slices
   individual cards out of multi-card source PDFs in `fixture/`.

2. **Card data + assets → final PNG (per-card, every run).**
   `reconstruct_card.py` consumes a tab-separated card data file (header row,
   columns matching `cards_with_processed_columns.txt`) and a per-card photo
   from `fixture/low quality decipher images/` (keyed by `CollectorsInfo`,
   falling back to `ImageFile`; dilemmas additionally fall back to
   `../webula/public/cardimages/`). It branches on the row's `Type` to a
   per-type renderer (`render_personnel`, `render_ship`, `render_dilemma`,
   `render_event`, ...) which pastes assets from the relevant `extracted/<…>/`
   tree and draws text with the fonts in `fonts/`.

   All layout constants are written in 300-DPI design space and scaled at
   runtime via `S()` (coords/sizes), `gfont()` (fonts), `scale_asset()` /
   `paste_rgba()` (images). `SCALE = dpi/300`. Never paste a raw asset —
   it will be wrong at 600/800 DPI.

### Cross-cutting renderer concerns

These rules span card types; keep them in mind before adding a new renderer.
The authoritative notes are in `reconstruct_card.md` and `notes.md`:

- **NAME / TITLE split** is not in the data. `reconstruct_card.py` OCRs the
  name band of the scan and fuzzy-aligns the known tokens to find the split
  point. Per-card overrides live in `fixture/name_title_map.tsv` (also handles
  title-less ship names like "U.S.S. Akira"). Low-confidence splits are
  reported at the end of a run.
- **Card name + unique dot** are drawn by a single helper (`draw_card_name`).
  The unique dot shows on Personnel/Ship/Event/Equipment, NOT on
  Interrupt/Dilemma — pass `unique=False` there regardless of the data field.
- **Lower text block** (`draw_textflow`) is a rich flow with bold/medium/italic
  runs, inline `[TNG]`-style icons from `extracted/icons/inline/`, ragged-right
  alignment, uniform line height, and auto-shrink for dense game text. Keyword
  text continues inline with game text on the same line unless the game text
  begins with an "Order -"-style bold lexeme.
- **Bold game text is DILEMMA-ONLY** and not present in any data source.
  `detect_bold_gametext.py` recovers it per-card from the scans (per-card
  self-calibration is mandatory — these are virtual cards from 10+ years with
  no common print/scan process; cross-card weight scales don't exist).
  The detector is recall-biased; hand corrections live in
  `fixture/gametext_bold_overrides.tsv` and are merged at write time so they
  survive re-runs. The renderer reads `fixture/gametext_bold.tsv` as the
  `GAMETEXT_MARKUP` sidecar.
- **Scope today:** every card type in the data has a renderer
  (Personnel, Ship, Dilemma, Event, Interrupt, Equipment, Mission). Personnel
  and Ship share `render_card`, which dispatches via `affil_cfg()` — all 11
  affiliations present in the data (Federation, Klingon, Romulan, Bajoran,
  Cardassian, Dominion, Borg, Ferengi, Starfleet, Vidiian, Non-Aligned) have
  entries in `AFFIL_CFG`. Per-affil ship cfg currently inherits Federation
  defaults for the non-Fed affiliations; personnel cfg is genuinely calibrated
  per affil. The other types don't branch on affiliation.

### Data files

- `cards_with_processed_columns.txt` — master 28-column TSV of all cards.
- `fixture/deck_*.txt` — per-type subsets derived from
  `reference_export/reference_deck_input.txt` (collector codes + names) by
  normalising the code to `<set><rarity><000-padded number>` to match
  `CollectorsInfo`, then grouping by `Type`.
- `fixture/2eed_hires.pdf`, `fixture/ReturntoGrace_hires.pdf`,
  `fixture/2e_eratta_sample.pdf` — large source PDFs, **git-ignored** (kept
  locally only). `extract_cards.py` slices cards from the grid layouts.

## Things not to re-investigate

- **300 DPI is the source ceiling.** Every `templates/*.psd` is exactly
  736×1029 px @ 300 DPI ("HD" is a product name). Chrome layers are plain
  raster `PixelLayer`s with no Smart Objects or vector fills, so re-extracting
  "at higher resolution" yields byte-identical assets. Runtime LANCZOS upscale
  is the chosen approach. (Genuine alternatives, if ever needed:
  super-resolution baked per-DPI, vectorising key elements, or sourcing higher-
  res templates.)
- **`2eed_hires.pdf` is OUT OF SCOPE as a bold-detection source** even though
  its bold is recoverable from font names. It covers very few of the cards we
  care about. Do not propose it as an improvement over the scan-based detector
  until the in-script disambiguation priors and per-line calibration in
  `detect_bold_gametext.py` are genuinely exhausted.
- **Ship staffing dark socket disc** is authentic, not an artefact (visible on
  printed cards). Keying it transparent was tried and rejected — the scan's
  own printed star ghosts through.
- **Card_Background chrome luminance varies row-to-row.** `extract_socket.py`
  crops tightly and tapers the right edge of its alpha for this reason;
  pasting slot 2's full chrome strip onto slot 3 produces a visible darker
  patch.
- **Only Federation has a baked socket in `Card_Background`.** Audited all
  affiliation PSDs at slot 1/2/3 positions: only `2e HD Federation v1.psd`'s
  Layer_11 has the socket disc rasterised — `extract_socket.py` crops it. The
  other seven affiliation PSDs render their sockets via vector mask + PS layer
  effects (color overlay + stroke) on per-slot `Base` layers. Those layers
  show as zero-alpha through `psd-tools.composite()` AND `psd2svg`, because
  neither simulates Photoshop layer effects.

## Headless Photopea for layer-effect rendering

For anything that depends on PSD layer effects (sockets, the affiliation
strip icons in the mission PSD, era-affiliation glyphs, etc.) the working
pipeline is **headless Chromium + Photopea iframe API via Playwright**:

- `bake_sockets.py` is the reference implementation. It opens each
  affiliation PSD by posting its bytes into Photopea, isolates one named
  layer (`Slot 1/Base`), and `app.activeDocument.saveToOE("png")`s the
  canvas. Output is trimmed + right-edge alpha-tapered the same way
  `extract_socket.py` finishes Federation's socket.
- Deps: `pip install playwright && playwright install chromium`. Launch
  with `args=["--use-gl=swiftshader"]` — software WebGL is required for
  layer effects to render correctly in headless mode.
- Photopea responds to `postMessage` with `'done'` after each script and
  with `ArrayBuffer` PNG bytes after `saveToOE`. The wrapper page in
  `bake_sockets.py` queues both into `window._mm` / `window._bins` so
  Playwright can poll them.
- Photopea substitutes missing fonts (FuturiCondensed → DejaVuSans) on
  load — fine for bakes that don't render text, **not** fine if we ever
  try to use Photopea for whole-card rendering.
- Throughput is ~30s for Photopea startup, then ~3s per layer bake.

Other paths we tried that don't work for layer effects: `psd-tools`,
`psd2svg`, ImageMagick. Reserve Photopea-style baking for layers where
`psd-tools` returns zero-alpha; for everything that *is* a real raster
in the PSD, keep using `extract_layout_from_template.py`.
