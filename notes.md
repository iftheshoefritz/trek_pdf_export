# Trek export — working notes

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
python3 reconstruct_card.py      # render fixture/reconstructed_WYLB078.png
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

## Flavor text

Not currently rendered. `cards_with_processed_columns.txt` has 28 columns and
none of them is flavor text. If/when a flavor source is added, plumb it into
`reconstruct_card.py` below the game text block.
