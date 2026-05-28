#!/usr/bin/env python3
"""
create_cards_pdf.py — Assemble a 9-card-per-page PDF from card images
using a hardcoded A4 3x3 grid layout at 800 DPI.

The script does not scale or alter the images. Each image must already be
the exact pixel size of a card cell in the reference PDF.

Usage:
    python3 create_cards_pdf.py --deck /path/to/deck.txt --images /path/to/cards --out out.pdf

Options:
    --deck PATH            Deck list. One card per line: card ID first,
                           optionally followed by whitespace/tab and the
                           card title (used only with --match-title).
    --images PATH          Directory of card images (PNG/JPG/etc.).
    --out PATH             Output PDF path.
    --match-title          When a deck ID has no matching image, fall back to
                           matching by title slug. Lets you use a reprint
                           image (different card ID, same title) for a card
                           whose canonical-ID image isn't available. The
                           deck line must include the title after the ID for
                           this to work.
    --verbose              Print layout info.

Requires:
    - Pillow
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from PIL import Image

from extract_cards import slugify


SUPPORTED_EXTS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp"}

# Hardcoded layout from reference_export/reference_export.pdf at 800 DPI.
# Page size: A4 @ 800 DPI -> 6615 x 9355 px.
OUTPUT_DPI = 800
PAGE_SIZE = (6615, 9355)
CARD_BOXES = [
    (343, 532, 2328, 3304),
    (2330, 532, 4315, 3304),
    (4318, 532, 6303, 3304),
    (343, 3307, 2328, 6079),
    (2330, 3307, 4315, 6079),
    (4318, 3307, 6303, 6079),
    (343, 6081, 2328, 8854),
    (2330, 6081, 4315, 8854),
    (4318, 6081, 6303, 8854),
]


def _load_deck(deck_path: Path) -> list[tuple[str, str | None]]:
    """Parse a deck file. Each non-blank line: card ID (first whitespace token)
    optionally followed by the card title. Title is None when absent."""
    entries: list[tuple[str, str | None]] = []
    with deck_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped:
                continue
            parts = stripped.split(None, 1)
            card_id = parts[0]
            title = parts[1].strip() if len(parts) > 1 else None
            entries.append((card_id, title))
    return entries


def _normalize_card_id(card_id: str) -> str:
    match = re.match(r"^([0-9]+[A-Z]+)([0-9]+)$", card_id)
    if not match:
        return card_id
    prefix, number = match.groups()
    return f"{prefix}{int(number)}"


def _index_images(images_dir: Path) -> tuple[dict[str, Path], dict[str, Path]]:
    """Build two indexes keyed by, respectively, normalized card ID and title
    slug, both derived from the image filename's stem ({id}_{slug}). First
    match wins (sorted filename order) for each key."""
    if not images_dir.is_dir():
        raise NotADirectoryError(f"Images path is not a directory: {images_dir}")
    images = [p for p in images_dir.iterdir() if p.suffix.lower() in SUPPORTED_EXTS]
    images = sorted(images)
    by_id: dict[str, Path] = {}
    by_title: dict[str, Path] = {}
    for img in images:
        stem = img.stem
        if "_" in stem:
            card_id, title_slug = stem.split("_", 1)
        else:
            card_id, title_slug = stem, ""
        by_id.setdefault(_normalize_card_id(card_id), img)
        if title_slug:
            by_title.setdefault(title_slug, img)
    return by_id, by_title


def _fit_image_to_cell(img: Image.Image, target_size: tuple[int, int]) -> Image.Image:
    target_w, target_h = target_size
    if img.size == (target_w, target_h):
        return img
    delta_w = abs(img.size[0] - target_w)
    delta_h = abs(img.size[1] - target_h)
    if delta_w > 10 or delta_h > 10:
        raise ValueError(
            f"Image size {img.size} does not match cell size {(target_w, target_h)}"
        )

    if img.size[0] > target_w or img.size[1] > target_h:
        left = max(0, (img.size[0] - target_w) // 2)
        top = max(0, (img.size[1] - target_h) // 2)
        right = left + target_w
        bottom = top + target_h
        return img.crop((left, top, right, bottom))

    mode = "RGBA" if img.mode in ("RGBA", "LA") else "RGB"
    background = (255, 255, 255, 0) if mode == "RGBA" else (255, 255, 255)
    canvas = Image.new(mode, (target_w, target_h), background)
    offset = ((target_w - img.size[0]) // 2, (target_h - img.size[1]) // 2)
    if img.mode in ("RGBA", "LA"):
        canvas.paste(img, offset, img)
    else:
        canvas.paste(img, offset)
    return canvas


def _paste_image(page: Image.Image, img: Image.Image, bbox: tuple[int, int, int, int]) -> None:
    left, top, right, bottom = bbox
    target_w = right - left
    target_h = bottom - top
    fitted = _fit_image_to_cell(img, (target_w, target_h))
    if fitted.mode in ("RGBA", "LA"):
        page.paste(fitted, (left, top), fitted)
    else:
        page.paste(fitted, (left, top))


def main() -> int:
    parser = argparse.ArgumentParser(description="Assemble a 3x3 card PDF from images")
    parser.add_argument("--deck", type=Path, required=True)
    parser.add_argument("--images", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--match-title", action="store_true",
        help="When a deck ID has no image, fall back to matching by the "
             "card's title slug (lets you substitute a reprint with a "
             "different ID but the same title).",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    deck_entries = _load_deck(args.deck)
    if not deck_entries:
        raise FileNotFoundError("Deck file contained no card IDs.")

    index_by_id, index_by_title = _index_images(args.images)
    resolved_images: list[Path] = []
    for card_id, title in deck_entries:
        img_path = index_by_id.get(_normalize_card_id(card_id))
        if img_path is None and args.match_title and title:
            title_slug = slugify(title)
            img_path = index_by_title.get(title_slug)
            if img_path is not None:
                print(
                    f"Substituting {img_path.stem.split('_', 1)[0]} for "
                    f"{card_id} (matched by title '{title}')",
                    file=sys.stderr,
                )
        if img_path is None:
            print(f"Missing card image for ID: {card_id}", file=sys.stderr)
            continue
        resolved_images.append(img_path)

    if not resolved_images:
        raise FileNotFoundError("No matching images found for deck IDs.")

    page_size = PAGE_SIZE
    boxes = CARD_BOXES

    if args.verbose:
        print(f"Page size: {page_size[0]}x{page_size[1]} px @ {OUTPUT_DPI} DPI")
        print(f"Images: {len(resolved_images)}")

    pages: list[Image.Image] = []
    for i in range(0, len(resolved_images), 9):
        page = Image.new("RGB", page_size, (255, 255, 255))
        for slot in range(9):
            idx = i + slot
            if idx >= len(resolved_images):
                break
            img_path = resolved_images[idx]
            try:
                img = Image.open(img_path)
            except OSError as e:
                raise RuntimeError(f"Failed to open {img_path}: {e}") from e
            _paste_image(page, img, boxes[slot])
        pages.append(page)

    first, rest = pages[0], pages[1:]
    first.save(args.out, "PDF", save_all=True, append_images=rest, resolution=OUTPUT_DPI)
    if args.verbose:
        print(f"Wrote {len(pages)} page(s) to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
