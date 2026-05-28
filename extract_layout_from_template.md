# Star Trek CCG 2E HD Card Template Extractor

A Python script that reads a faction PSD template (e.g. `2e HD Federation v1.psd`)
and produces a renderer-friendly JSON layout spec plus a folder of extracted PNG assets.

## Usage

```bash
pip install psd-tools
python extract_psd.py "2e HD Federation v1.psd" ./extracted/federation
```

## Output

```
extracted/federation/
├── spec.json          # full layout spec (see below)
├── preview.png        # rendered template with intro overlay hidden
└── assets/            # one PNG per pixel layer, mirroring layer hierarchy
    ├── Black_Border.png
    ├── Card_Background/
    │   └── Layer_11.png
    ├── Skills_and_Flavor_Text/Personnel/Skill_1/Dot.png
    ├── Staffing_and_Attributes/Ship/Staffing/Slot_1/Command.png
    └── ...
```

## `spec.json` schema

Top level:

```jsonc
{
  "source_psd": "2e HD Federation v1.psd",
  "canvas": {
    "width": 736, "height": 1029,
    "dpi": [300.0, 300.0],
    "size_inches": [2.45, 3.43]
  },
  "fonts_required": [
    "CrilleeBT-Italic",
    "FuturaLT-Condensed",
    "FuturaLT-CondensedBold",
    "FuturaLT-CondensedBoldOblique",
    "FuturaLT-CondensedLightObl",
    "FuturiCondensedBoldSWFTE",
    "FuturiCondensedLightObliqueSWFTE"
  ],
  "layers": [ /* recursive layer tree, see below */ ]
}
```

### Layer node (common fields)

```jsonc
{
  "name": "Game Text",
  "path": "Skills and Flavor Text > Personnel > Game Text",
  "kind": "text" | "pixel" | "group",
  "visible": true,
  "bbox": [left, top, right, bottom],   // PSD pixel coords; may be null for empty groups
  "size": [width, height]
}
```

### Additional fields by kind

**`"kind": "text"`** adds:

```jsonc
{
  "text": "Commander: U.S.S. Odyssey. While this personnel is...",
  "font": "FuturaLT-CondensedBold",
  "font_size": 29.16667,
  "color": [R, G, B, A],                 // 0–255 ints
  "justification": "left",                // left | right | center | justify_*
  "font_caps": "normal",                  // normal | small_caps | all_caps
  "faux_bold": false,
  "faux_italic": false,
  "tracking": 0,
  "leading": null
}
```

**`"kind": "pixel"`** adds:

```jsonc
{
  "asset_file": "assets/Skills_and_Flavor_Text/Personnel/Skill_1/Dot.png"
}
```

**`"kind": "group"`** adds:

```jsonc
{
  "children": [ /* nested layer nodes */ ]
}
```

## Notes for downstream renderer

- The canvas is **300 DPI** but we want **800 DPI** output. Multiply every
  bbox, font_size, and the canvas dimensions by `800/300 ≈ 2.667` when rendering.
  Alternatively, render at native 300 DPI and upscale only the final art crop.
- Many pixel-layer "Slot" groups have multiple mutually exclusive children
  (e.g. `Staffing > Slot 2` contains Maquis / Earth / Voyager / DS9 / TNG / TOS
  icons). The `visible` flag in the source PSD reflects the example card; your
  renderer should toggle these per the card data in the database.
- Empty groups have `bbox: null` because their children are all hidden in the
  source PSD — that's fine, those groups have meaningful children regardless.
- Text bboxes are the bounding box of the **text frame**, not the text itself.
  When re-rendering with PIL, draw text into a frame of `(width, height)`
  starting at `(left, top)` and let the font metrics handle ascenders/descenders.
- The "Intro" group should always be hidden when rendering real cards — it's
  the welcome screen baked into the template.

## Quirks of psd-tools handled here

- psd-tools wraps strings and integers in custom `String`/`Integer` classes.
  `str(s)` adds quote marks, `isinstance(i, int)` returns False. The script
  uses `.value` for strings and `int(...)` coercion for ints.
- Font names live in the layer's `resource_dict.FontSet`, NOT in `engine_dict`.
- DPI lives in image resource block 1005 which psd-tools doesn't expose
  cleanly, so the script parses the binary directly.
- PSD `FillColor` of type 1 stores values as `[A, R, G, B]` floats in [0,1].
  Verified by pixel-sampling rendered output.
