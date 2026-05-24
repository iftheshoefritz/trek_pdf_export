#!/usr/bin/env python3
"""
extract_cards.py — Extract individual card images from Star Trek CCG-style
PDFs laid out in a grid of up to 3 columns by 3 rows per page.

Usage:
    # Single PDF:
    python3 extract_cards.py <input.pdf> [options]

    # Whole directory of PDFs (recommended for batch jobs):
    python3 extract_cards.py <directory>  [options]

Options:
    --out OUTDIR        Output directory. Defaults to <pdfstem>_cards/ for a
                        single PDF, or <directory>/extracted/ for a folder.
    --per-pdf-subdir    When input is a directory, write each PDF into its own
                        sub-folder under the output root (default: flat).
    --dpi N             Render DPI (default 300; 800 for MPC print orders).
    --no-round          Skip the rounded-corner alpha mask; keep rectangles.
    --snap-black        Snap near-neutral very-dark pixels to pure RGB(0,0,0)
                        in the title and rules-text bands. Recommended for
                        print orders (MPC etc.) to keep small text K-only and
                        avoid CMYK mis-registration fringing. Leaves card
                        artwork and anti-aliased edges untouched.
    --bleed [MM]        Synthesize a rectangular bleed area around each card
                        by sampling the border colour and extending it
                        outward. Required for on-demand printing. Pass
                        --bleed for 3 mm, or --bleed N for a custom amount.
                        Output is rectangular (no rounded corners) — print
                        services cut the corners at the press. Not
                        recommended for "borderless" card sets.
    --id-sequence X:N   Force sequential card IDs PREFIX{NN}. Rarely needed —
                        the script auto-detects the prefix via OCR voting for
                        raster-only PDFs.
    --pages 1-3         Process only a page range (default: all pages).
    --verbose / -v      Print measurements and per-card info.

Requires:
    - poppler        (`pdftoppm`, `pdftotext`)   brew install poppler
    - tesseract      (only used for raster-only PDFs) brew install tesseract
    - Pillow, NumPy:                              pip install Pillow numpy

What it does, per page:
    1. Renders the page at the requested DPI with pdftoppm.
    2. Detects the card grid. The grid bounding box is found from the page
       margins; column count is anchored on the page width (a single card
       occupies ~30% of a US-letter page width regardless of DPI) and row
       count then follows from the card aspect ratio. Both dimensions can be
       1, 2, or 3 — the last page of a set may be short either way.
       Interior seams are refined via the four-card-intersection "white
       stars" where rounded corners pull away from each other; missing
       stars fall back to symmetric prediction.
    3. Skips non-card pages (cover sheets, tables of contents) using
       aspect-ratio and ID-count heuristics.
    4. Crops each card and (by default) applies a rounded-corner alpha mask.
       Corner radius is the median of the white-star measurements, or a
       typical 3%-of-short-side default when no stars are found.
    5. Pulls card titles and IDs (e.g. "66 V 9", "12 BP 4", "0 VP 293",
       "1B1") from `pdftotext -bbox-layout`, assigning each word to a grid
       cell by its actual page coordinate, and names output files
       {SET}{NUM:02d}_{slug}.png. IDs are detected per cell, so a single
       PDF containing cards from many different sets (e.g. an errata
       compilation) names each card with its own canonical prefix.
    6. If the PDF has no extractable text (some PDFs flatten everything
       into raster images), tesseract OCRs the ID strip of every extracted
       card. Per-card reads are noisy, but the set prefix is the same on
       every card in a deck, so a simple majority vote across all cards
       reliably recovers the prefix. Card numbers are then assigned
       sequentially in reading order across pages. Note: this OCR fallback
       assumes one set per PDF and won't work correctly on mixed-set
       raster PDFs.

Batch behaviour:
    When the input is a directory, every .pdf file in it is processed in
    turn, in alphabetical order. By default all cards are written into the
    output root; use --per-pdf-subdir to create a sub-folder per PDF.
    Filenames collide deliberately: if two PDFs extract the same canonical
    card ID, the later one overwrites the earlier one. This means an errata
    PDF named e.g. zzz_errata.pdf can be dropped alongside the regular set
    PDFs and its updated card images will replace the originals.
    Errors are collected per-file so one bad PDF doesn't abort the batch.
    The exit code is 1 if any PDF in the batch produced errors.

Recommended invocation for MPC-bound print orders:
    python3 extract_cards.py /path/to/pdfs --dpi 800 --snap-black --bleed
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image, ImageDraw

# --- constants -------------------------------------------------------------

# Cards are laid out in a grid of up to 3 columns by 3 rows. The last page
# of a set may be short in either dimension (a single trailing card, a half
# row, etc.).
MAX_GRID_COLS = 3
MAX_GRID_ROWS = 3

# Standard trading-card aspect ratio (width / height). Used to detect how many
# rows of cards are present on a page from the detected grid bounding box.
CARD_ASPECT_W_OVER_H = 2.5 / 3.5  # ≈ 0.714

# Fraction of the letter-sized PDF page width occupied by one card. A standard
# trading card is 2.5" = 180pt wide and the page is 612pt wide, so a card is
# about 29.4% of page width regardless of render DPI. Used to disambiguate the
# column count when the detected grid bounding box could correspond to several
# (n_cols, n_rows) configurations with the same cell aspect ratio.
CARD_W_PAGE_FRAC = 180.0 / 612.0

# Threshold for "near-white" page background (0..255 grayscale).
WHITE_THRESHOLD = 240

# Threshold for "any ink" — used to find page margins and corner curves.
INK_THRESHOLD = 240

# Card ID pattern, e.g. "66 V 9", "0 VP 293", "12 BP 4", "3 AP 17", "1B1".
# The middle group is any 1-2 capital letters. Many community sets use a
# different convention — V (virtual), BP, AP, VP, B, etc. — so we don't lock
# down the prefix. Whitespace between components is optional ("1B1" is also
# a real format).
#
# Risk: high-DPI rasterization sometimes makes pdftotext split stat-bar
# words like "INTEGRITY 6  CUNNING 5  STRENGTH 5" into single capitals plus
# numbers, producing patterns like "6 I 5" that match this regex. We rely on
# the per-cell ID search being bottom-up: the canonical card ID always sits
# at the very bottom of the cell, below any stat-bar text, so the bottommost
# match wins.
CARD_ID_PATTERN = re.compile(r"\b(\d{1,3})\s*([A-Z]{1,2})\s*(\d{1,3})\b")


# --- data containers -------------------------------------------------------

@dataclass
class PageGeometry:
    """The detected card grid for one page."""
    x_edges: list[int]    # n_cols+1 values: left edge, vertical seams, right edge
    y_edges: list[int]    # n_rows+1 values: top edge, horizontal seams, bottom edge
    n_cols: int           # detected number of card columns (1, 2, or 3)
    n_rows: int           # detected number of card rows (1, 2, or 3)
    corner_radius: int    # in pixels
    page_width: int
    page_height: int

    def cell(self, row: int, col: int) -> tuple[int, int, int, int]:
        """Bounding box (left, top, right, bottom) of cell at (row, col), 0-indexed."""
        return (self.x_edges[col], self.y_edges[row],
                self.x_edges[col + 1], self.y_edges[row + 1])


@dataclass
class CardMeta:
    """Identifying info for one card, pulled from pdftotext."""
    card_id: Optional[str]   # canonical id like "66V09"
    set_prefix: Optional[str]  # e.g. "66V"
    number: Optional[int]    # e.g. 9
    title: Optional[str]     # e.g. "Ghorusda"


# --- shell helpers ---------------------------------------------------------

def _run(cmd: list[str], *, capture_stderr: bool = False) -> str:
    """Run a subprocess; return stdout. Raises on non-zero exit."""
    proc = subprocess.run(
        cmd,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if capture_stderr:
        return proc.stdout + proc.stderr
    return proc.stdout


def check_tools() -> None:
    """Verify pdftoppm and pdftotext are on PATH."""
    missing = []
    for tool in ("pdftoppm", "pdftotext"):
        try:
            subprocess.run([tool, "-v"], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False)
        except FileNotFoundError:
            missing.append(tool)
    if missing:
        sys.exit(
            f"Error: required tool(s) not found on PATH: {', '.join(missing)}\n"
            f"On macOS install with:  brew install poppler"
        )


def get_page_count(pdf_path: Path) -> int:
    """Read the page count from pdfinfo. Falls back to pdftoppm trial if needed."""
    try:
        out = _run(["pdfinfo", str(pdf_path)])
        for line in out.splitlines():
            if line.startswith("Pages:"):
                return int(line.split(":", 1)[1].strip())
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        pass
    # Fallback: render until we get an error, counting pages
    return 0  # caller can handle 0 by trying pages sequentially


# --- geometry detection ----------------------------------------------------

def find_white_star(arr: np.ndarray, x_hint: int, y_hint: int,
                    search: int) -> Optional[tuple[float, float, int]]:
    """
    Find the seam intersection (white "star") at a 4-card grid intersection.

    At the intersection, four cards' rounded corners pull away from each other,
    exposing a diamond-shaped patch of white page background. At higher DPI
    this also has thin white "lobes" along the seam lines between cards (where
    the inter-card border is too narrow to fully cover the gap).

    Strategy:
        1. Find the nearest white pixel to the hint.
        2. Find the longest horizontal white run through that row — its
           midpoint is the seam x.
        3. Find the longest vertical white run through that column — its
           midpoint is the seam y.
        4. The "corner radius" is half the shorter of the two extents at the
           seam center, which equals the white diamond's half-width and
           thus the corner radius (since the diamond is bounded by four
           quarter-circles).

    This avoids the bias introduced by long thin lobes that throw off a
    connected-component centroid.

    Returns (cx, cy, corner_radius) or None if no plausible star found.
    """
    h, w = arr.shape

    # Step 1: ensure hint is on a white pixel; if not, search a small
    # neighbourhood for one. The neighbourhood is sized to handle moderate
    # per-page print alignment drift.
    drift = max(20, search // 3)
    y0 = max(0, y_hint - drift)
    y1 = min(h, y_hint + drift + 1)
    x0 = max(0, x_hint - drift)
    x1 = min(w, x_hint + drift + 1)
    region = arr[y0:y1, x0:x1]
    white = region >= WHITE_THRESHOLD
    if not white.any():
        return None

    # Pick the white pixel closest to the hint
    ys, xs = np.where(white)
    cy_local = y_hint - y0
    cx_local = x_hint - x0
    dists = np.hypot(ys - cy_local, xs - cx_local)
    idx = int(np.argmin(dists))
    seed_y = int(ys[idx]) + y0
    seed_x = int(xs[idx]) + x0

    # Step 2: at the seed row, find the horizontal white run containing seed_x.
    row = arr[seed_y, :]
    left = seed_x
    while left > 0 and row[left - 1] >= WHITE_THRESHOLD:
        left -= 1
    right = seed_x
    while right + 1 < w and row[right + 1] >= WHITE_THRESHOLD:
        right += 1
    h_extent = right - left + 1
    h_mid = (left + right) / 2

    # Step 3: at the seed column, find the vertical white run containing seed_y.
    col = arr[:, seed_x]
    top = seed_y
    while top > 0 and col[top - 1] >= WHITE_THRESHOLD:
        top -= 1
    bottom = seed_y
    while bottom + 1 < h and col[bottom + 1] >= WHITE_THRESHOLD:
        bottom += 1
    v_extent = bottom - top + 1
    v_mid = (top + bottom) / 2

    # Iterate once: use the midpoints to find better seed, then re-measure.
    seed_x2 = int(round(h_mid))
    seed_y2 = int(round(v_mid))
    if 0 <= seed_y2 < h and 0 <= seed_x2 < w and arr[seed_y2, seed_x2] >= WHITE_THRESHOLD:
        row2 = arr[seed_y2, :]
        left = seed_x2
        while left > 0 and row2[left - 1] >= WHITE_THRESHOLD:
            left -= 1
        right = seed_x2
        while right + 1 < w and row2[right + 1] >= WHITE_THRESHOLD:
            right += 1
        h_extent = right - left + 1
        h_mid = (left + right) / 2

        col2 = arr[:, seed_x2]
        top = seed_y2
        while top > 0 and col2[top - 1] >= WHITE_THRESHOLD:
            top -= 1
        bottom = seed_y2
        while bottom + 1 < h and col2[bottom + 1] >= WHITE_THRESHOLD:
            bottom += 1
        v_extent = bottom - top + 1
        v_mid = (top + bottom) / 2

    # Reject if the midpoints are too far from the original hint (probably
    # a different white region entirely).
    tolerance = max(20, search // 3)
    if abs(h_mid - x_hint) > tolerance or abs(v_mid - y_hint) > tolerance:
        return None

    # Reject if either extent is implausibly small (must be at least a few
    # pixels of white to plausibly be a star).
    if h_extent < 5 or v_extent < 5:
        return None

    # Corner radius: half the shorter extent. The white diamond's bounding box
    # at the seam center is 2R × 2R for corner radius R, so half the extent
    # at the center IS the corner radius. We take the min in case one axis has
    # a long lobe (in which case its extent overestimates R).
    corner_radius = int(round(min(h_extent, v_extent) / 2))

    return float(h_mid), float(v_mid), corner_radius


def find_grid_outer_bounds(arr: np.ndarray) -> tuple[int, int, int, int]:
    """
    Find the outer bounding box of the card grid by detecting the white page
    margins on each side.

    Returns (left, top, right, bottom) — pixel coordinates inclusive on the
    grid edge.

    Handles two complications:

    1. At high DPI, the rounded-corner gaps between card rows can leave a
       narrow strip of fully-white rows (where row_mean briefly reaches 255).
       We must treat the grid as a single block even when small white gaps
       split it.

    2. Some PDFs have a footer line (e.g. a copyright notice) below the grid.
       It appears as a small dark "island" separated from the main grid by a
       wide white gap.

    Algorithm:
        - Find the leftmost/rightmost non-white columns (page-edge margins
          are pure white and don't get broken by anything).
        - Find non-white row runs.
        - Merge runs separated by small white gaps (< 0.5% of page height
          ≈ 16 px at 300 DPI), since those are inter-card-row seams, not
          structural gaps.
        - The first merged run is the card grid. Any later runs are headers/
          footers and are excluded.
    """
    h, w = arr.shape

    # Find left/right margins via column-mean. Page margins are pure white.
    col_mean = arr.mean(axis=0)
    left = 0
    for x in range(w):
        if col_mean[x] < 245:
            left = x
            break
    right = w - 1
    for x in range(w - 1, -1, -1):
        if col_mean[x] < 245:
            right = x
            break

    # Find non-white row runs.
    row_mean = arr.mean(axis=1)
    nonwhite = row_mean < 245
    raw_runs: list[tuple[int, int]] = []
    in_run = False
    rstart = 0
    for y in range(h):
        if nonwhite[y] and not in_run:
            rstart = y; in_run = True
        elif not nonwhite[y] and in_run:
            raw_runs.append((rstart, y - 1))
            in_run = False
    if in_run:
        raw_runs.append((rstart, h - 1))

    if not raw_runs:
        return left, 0, right, h - 1

    # Merge runs separated by small white gaps. A 0.5% page-height threshold
    # accommodates the largest reasonable rounded-corner gap (~40 px @ 300 DPI,
    # ~80 px @ 600 DPI). Anything bigger is a structural gap between the grid
    # and a header/footer.
    merge_gap = max(8, h // 200)
    merged: list[tuple[int, int]] = [raw_runs[0]]
    for r in raw_runs[1:]:
        prev = merged[-1]
        if r[0] - prev[1] <= merge_gap:
            merged[-1] = (prev[0], r[1])
        else:
            merged.append(r)

    # The card grid is the first (topmost) merged run — these PDFs never have
    # a header above the grid. Footers, if any, are later runs.
    top, bottom = merged[0]

    return left, top, right, bottom


def detect_page_geometry(arr: np.ndarray, verbose: bool = False) -> PageGeometry:
    """Detect the card grid for one page (1-3 columns by 1-3 rows)."""
    h, w = arr.shape

    # Step 1: outer bounds
    left, top, right, bottom = find_grid_outer_bounds(arr)
    if verbose:
        print(f"    outer bounds: x={left}-{right}, y={top}-{bottom}")

    grid_w = right - left + 1
    grid_h = bottom - top + 1

    # Step 2: figure out the grid shape.
    # Column count is anchored on the page width: one card occupies
    # CARD_W_PAGE_FRAC of the page width at any DPI (both grid_w and w come
    # from the same pixel array). This anchor matters because for grids whose
    # bounding box is itself card-aspect (e.g. a single card alone on a page,
    # or a square-ish 3x3 grid), multiple (n_cols, n_rows) fits give the same
    # cell aspect ratio and the column count can't be derived from grid shape
    # alone.
    # Row count then follows from the (now known) cell width: cell_h must be
    # cell_w / CARD_ASPECT_W_OVER_H, and n_rows is grid_h divided by that.
    expected_card_w = w * CARD_W_PAGE_FRAC
    n_cols_float = grid_w / expected_card_w
    n_cols = max(1, min(MAX_GRID_COLS, round(n_cols_float)))
    card_w_predicted = grid_w / n_cols
    expected_card_h = card_w_predicted / CARD_ASPECT_W_OVER_H
    n_rows_float = grid_h / expected_card_h
    n_rows = max(1, min(MAX_GRID_ROWS, round(n_rows_float)))
    card_h_predicted = grid_h / n_rows
    if verbose:
        print(f"    detected {n_cols} col(s) x {n_rows} row(s) "
              f"(grid_w/expected_card_w = {n_cols_float:.2f}, "
              f"grid_h/expected_card_h = {n_rows_float:.2f})")

    # Step 3: predicted seam positions (assume uniform grid).
    predicted_v_seams = [
        round(left + (i + 1) * card_w_predicted - 0.5)
        for i in range(n_cols - 1)
    ]
    predicted_h_seams = [
        round(top + (i + 1) * card_h_predicted - 0.5)
        for i in range(n_rows - 1)
    ]

    # Step 4: refine seams via white-star detection at interior intersections.
    short_side = min(card_w_predicted, card_h_predicted)
    star_search = max(40, int(round(short_side / 6)))

    refined_v: list[Optional[float]] = [None] * len(predicted_v_seams)
    refined_h: list[Optional[float]] = [None] * len(predicted_h_seams)
    star_radii: list[int] = []
    n_star_checks = max(1, len(predicted_v_seams)) * max(1, len(predicted_h_seams))
    stars_found = 0
    for vi, vx in enumerate(predicted_v_seams):
        for hi, hy in enumerate(predicted_h_seams):
            star = find_white_star(arr, vx, hy, search=star_search)
            if star is None:
                continue
            cx, cy, radius = star
            stars_found += 1
            star_radii.append(radius)
            if refined_v[vi] is None:
                refined_v[vi] = cx
            else:
                refined_v[vi] = (refined_v[vi] + cx) / 2
            if refined_h[hi] is None:
                refined_h[hi] = cy
            else:
                refined_h[hi] = (refined_h[hi] + cy) / 2

    v_seams = [int(round(refined_v[i])) if refined_v[i] is not None
               else predicted_v_seams[i] for i in range(len(predicted_v_seams))]
    h_seams = [int(round(refined_h[i])) if refined_h[i] is not None
               else predicted_h_seams[i] for i in range(len(predicted_h_seams))]

    # Step 5: corner radius. When white-star intersections are found, the
    # median of their measured radii is reliable. When they aren't (e.g. pages
    # where the inter-card gaps are too tight or dim to register), fall back
    # to a typical default: real trading-card corners measure 2.5-3% of the
    # short side consistently across pages with reliable star data, so 3% is
    # a safe stand-in that keeps corners visually consistent with stars-found
    # pages instead of varying wildly per-page.
    if star_radii:
        radius = int(np.median(star_radii))
    else:
        radius = int(round(short_side * 0.03))

    if verbose:
        print(f"    stars found: {stars_found}/{n_star_checks}   "
              f"v_seams={v_seams}   h_seams={h_seams}")
        if stars_found < n_star_checks:
            print(f"    (fell back to symmetric prediction for missing seams)")
        if star_radii:
            print(f"    corner radius: {radius}px")
        else:
            print(f"    corner radius: {radius}px (typical default; no stars)")

    return PageGeometry(
        x_edges=[left] + v_seams + [right],
        y_edges=[top] + h_seams + [bottom],
        n_cols=n_cols,
        n_rows=n_rows,
        corner_radius=radius,
        page_width=w,
        page_height=h,
    )


def _clean_title(text: str) -> str:
    """
    Strip leading cost numbers and the • "unique" marker from a card title.

    Examples:
      "Ghosts of Reality"      -> "Ghosts of Reality"
      "5 Ghosts of Reality"    -> "Ghosts of Reality"
      "•Kira Nerys"            -> "Kira Nerys"
      "3 •Kira Nerys"          -> "Kira Nerys"
      "2 •U.S.S. Enterprise-D" -> "U.S.S. Enterprise-D"
    """
    s = text.strip()
    # Repeatedly strip leading cost digits and/or bullets.
    while True:
        new = s
        new = re.sub(r"^\d+\s*", "", new)   # leading cost number
        new = re.sub(r"^•\s*", "", new)     # leading bullet
        new = new.strip()
        if new == s:
            break
        s = new
    return s


# --- text parsing ----------------------------------------------------------

# Letter-sized page in points (what pdftotext -bbox uses).
PAGE_POINT_WIDTH = 612.0
PAGE_POINT_HEIGHT = 792.0


def _parse_bbox_xml(xml: str) -> list[tuple[float, float, float, float, str]]:
    """
    Parse the XML output of `pdftotext -bbox-layout`. Returns a list of words,
    each as (xMin, yMin, xMax, yMax, text) in PDF point coordinates.

    We use a tiny manual XML walk (no external XML parser) because the output
    is well-formed but contains XHTML entities and we only want the <word> tags.
    """
    words: list[tuple[float, float, float, float, str]] = []
    word_re = re.compile(
        r'<word\s+xMin="([\d.]+)"\s+yMin="([\d.]+)"'
        r'\s+xMax="([\d.]+)"\s+yMax="([\d.]+)">([^<]*)</word>',
    )
    for m in word_re.finditer(xml):
        x_min = float(m.group(1))
        y_min = float(m.group(2))
        x_max = float(m.group(3))
        y_max = float(m.group(4))
        text = (m.group(5)
                .replace("&amp;", "&")
                .replace("&lt;", "<")
                .replace("&gt;", ">")
                .replace("&quot;", '"')
                .replace("&apos;", "'"))
        words.append((x_min, y_min, x_max, y_max, text))
    return words


def get_page_words(pdf_path: Path, page_num: int) -> list[tuple[float, float, float, float, str]]:
    """Run pdftotext -bbox-layout for one page; return word boxes."""
    try:
        proc = subprocess.run(
            ["pdftotext", "-bbox-layout",
             "-f", str(page_num), "-l", str(page_num),
             str(pdf_path), "-"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            check=True, text=True, encoding="utf-8", errors="replace",
        )
        return _parse_bbox_xml(proc.stdout)
    except subprocess.CalledProcessError:
        return []


def parse_card_metadata_by_position(
    words: list[tuple[float, float, float, float, str]],
    geom: PageGeometry,
    page_image_w: int,
    page_image_h: int,
) -> list[Optional[CardMeta]]:
    """
    For each of the 9 grid cells, find the card ID and title by spatially
    grouping the bbox-layout words from pdftotext.

    Returns a list of 9 entries indexed by (row*3 + col); None where no card
    metadata was identified for that cell.
    """
    # Convert each word's center to pixel coordinates and figure out which
    # grid cell it falls into.
    scale_x = page_image_w / PAGE_POINT_WIDTH
    scale_y = page_image_h / PAGE_POINT_HEIGHT
    n_cells = geom.n_rows * geom.n_cols

    # Build per-cell word lists, preserving reading order (y, then x).
    cells: list[list[tuple[float, float, str]]] = [
        [] for _ in range(n_cells)
    ]
    for x_min, y_min, x_max, y_max, text in words:
        cx = (x_min + x_max) / 2 * scale_x
        cy = (y_min + y_max) / 2 * scale_y
        # Which row?
        row = None
        for r in range(geom.n_rows):
            if geom.y_edges[r] <= cy < geom.y_edges[r + 1]:
                row = r; break
        if row is None:
            continue
        col = None
        for c in range(geom.n_cols):
            if geom.x_edges[c] <= cx < geom.x_edges[c + 1]:
                col = c; break
        if col is None:
            continue
        cells[row * geom.n_cols + col].append((cy, cx, text))

    # Sort each cell's words top-to-bottom, left-to-right.
    for cell in cells:
        cell.sort(key=lambda t: (round(t[0]), t[1]))

    # Reconstruct lines per cell (group words whose y is within ~3 px).
    def lines_of(cell: list[tuple[float, float, str]]) -> list[tuple[float, str]]:
        """Group words in a cell into lines. Returns (y, line_text) sorted by y."""
        if not cell:
            return []
        lines: list[list[tuple[float, float, str]]] = []
        for w in cell:
            if not lines or abs(w[0] - lines[-1][-1][0]) > 6:
                lines.append([w])
            else:
                lines[-1].append(w)
        out: list[tuple[float, str]] = []
        for ln in lines:
            ln.sort(key=lambda t: t[1])
            txt = " ".join(w[2] for w in ln)
            out.append((ln[0][0], txt))
        return out

    # For each cell: find the card ID and the title.
    NON_TITLE = {
        "dilemma", "event", "interrupt", "equipment", "ship", "mission",
        "personnel", "site", "facility",
    }
    NOT_ENDORSED_RE = re.compile(r"NOT\s+ENDORSED", re.I)

    def looks_like_title(line_text: str) -> bool:
        s = line_text.strip().lstrip("•").strip()
        if not s:
            return False
        if NOT_ENDORSED_RE.search(s):
            return False
        if s.lower().strip(" .") in NON_TITLE:
            return False
        # Skip lines that are entirely numeric/stat content.
        if re.fullmatch(r"[\d\s+\-.>]+", s):
            return False
        # Skip lines that look like full sentences/rules (long, end with .)
        if len(s) > 60:
            return False
        return True

    metas: list[Optional[CardMeta]] = [None] * n_cells

    for i, cell in enumerate(cells):
        cell_lines = lines_of(cell)
        if not cell_lines:
            continue

        # Find card ID: scan lines bottom-up for CARD_ID_PATTERN, since the
        # canonical card ID always sits at the bottom of the card. This also
        # avoids picking up spurious matches from card text.
        set_prefix: Optional[str] = None
        number: Optional[int] = None
        id_line_y: Optional[float] = None
        for y, text in reversed(cell_lines):
            m = CARD_ID_PATTERN.search(text)
            if m:
                set_prefix = f"{m.group(1)}{m.group(2)}"
                number = int(m.group(3))
                id_line_y = y
                break

        # Find title: take the topmost line in the cell that looks like a title.
        title: Optional[str] = None
        for y, text in cell_lines:
            if id_line_y is not None and abs(y - id_line_y) < 4:
                continue  # don't reuse the ID line as title
            if looks_like_title(text):
                clean = _clean_title(text)
                if clean:
                    title = clean
                    break

        if set_prefix and number is not None:
            metas[i] = CardMeta(
                card_id=f"{set_prefix}{number:02d}",
                set_prefix=set_prefix,
                number=number,
                title=title,
            )
        elif title:
            metas[i] = CardMeta(
                card_id=None, set_prefix=None, number=None, title=title,
            )

    return metas


# --- rendering -------------------------------------------------------------

def render_page(pdf_path: Path, page_num: int, dpi: int,
                tmpdir: Path) -> Path:
    """Render a single PDF page to a JPEG using pdftoppm. Returns the path."""
    out_prefix = tmpdir / f"page{page_num:03d}"
    subprocess.run(
        ["pdftoppm", "-jpeg", "-jpegopt", "quality=95",
         "-r", str(dpi), "-f", str(page_num), "-l", str(page_num),
         str(pdf_path), str(out_prefix)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    # pdftoppm appends a zero-padded page number whose width depends on total
    # pages. Find the file it actually produced.
    matches = sorted(tmpdir.glob(f"page{page_num:03d}-*.jpg"))
    if not matches:
        raise RuntimeError(f"pdftoppm did not produce output for page {page_num}")
    return matches[0]


# --- filename helpers ------------------------------------------------------

def slugify(name: str) -> str:
    s = name.lower()
    s = s.replace("'", "").replace("’", "").replace(".", "")
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")


# --- main extraction -------------------------------------------------------

def snap_near_black_to_pure_black(
    img: Image.Image,
    max_channel: int = 50,
    neutrality_tolerance: int = 8,
    art_top_pct: float = 0.10,
    art_bottom_pct: float = 0.60,
) -> Image.Image:
    """
    Snap near-neutral very-dark pixels to pure RGB (0, 0, 0) in the title and
    rules-text regions of a card, leaving the artwork band alone.

    Why: card body text in many of these PDFs renders to RGB ≈ (33, 32, 33)
    rather than pure black. When sent to a CMYK press, near-black values
    become 4-colour black ("rich black"), which causes mis-registration
    fringing and softness on small text. Snapping these pixels to exact
    (0, 0, 0) gives the press a clean K-only signal.

    The filter is deliberately conservative AND spatially restricted:

      * Spatial: only the top strip of the card (above art_top_pct, ~title
        region) and the bottom strip (below art_bottom_pct, ~rules-box
        region) are eligible. The middle strip — the card artwork — is
        always left alone. This prevents dark areas of the photograph
        (starfields, hull shadows, etc.) from being crushed to flat black.
      * max_channel — only pixels where every channel is below this value
        are candidates. Default 50 keeps anti-aliased edges (which are
        brighter) untouched.
      * neutrality_tolerance — only pixels where the spread between max
        and min channel is small are candidates. Default 8 protects dark
        coloured frame ornaments and icons from being crushed.

    Default band boundaries (10% / 60%) were measured across mission,
    dilemma, event, personnel, and ship cards from four different STCCG-
    style PDFs. The card artwork in every type lies entirely between those
    Y positions; the title sits above; the rules box sits below.

    Operates on RGB or RGBA images; alpha is preserved.
    """
    has_alpha = img.mode == "RGBA"
    arr = np.array(img.convert("RGBA"))
    H = arr.shape[0]
    art_top = int(H * art_top_pct)
    art_bottom = int(H * art_bottom_pct)

    rgb = arr[..., :3].astype(np.int16)
    max_c = rgb.max(axis=-1)
    min_c = rgb.min(axis=-1)
    is_dark = max_c < max_channel
    is_neutral = (max_c - min_c) <= neutrality_tolerance
    candidate = is_dark & is_neutral

    # Restrict to title + rules-box bands; exclude the artwork band.
    allowed = np.ones_like(candidate, dtype=bool)
    allowed[art_top:art_bottom, :] = False
    mask = candidate & allowed

    arr[mask, 0] = 0
    arr[mask, 1] = 0
    arr[mask, 2] = 0
    out = Image.fromarray(arr)
    if not has_alpha:
        out = out.convert("RGB")
    return out


def _tesseract_available() -> bool:
    """Check whether tesseract is on PATH."""
    try:
        subprocess.run(["tesseract", "-v"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=False)
        return True
    except FileNotFoundError:
        return False


def ocr_card_id(card_img: Image.Image,
                bleed_px: int = 0) -> Optional[tuple[str, str, int]]:
    """
    Try to read the card ID printed at the bottom-right of a card image.

    Returns (number_str, prefix_letters, card_number) on success, or None.
    For example a card with the ID "61 V 7" printed on it returns
    ("61", "V", 7).

    If the card image has a synthesized bleed area, pass bleed_px so the
    crop targets the actual card content area instead of the bleed.

    Reliability: individual cards can fail to OCR (small text, low contrast,
    background interference). This function is designed to be called on many
    cards and have the results voted on by the caller — see ocr_set_prefix.
    """
    W, H = card_img.size
    # The ID label sits at the bottom-right of every card, inside a small
    # bright pill. Position is relative to the *card content area*, which is
    # inset by bleed_px on all sides when a bleed has been synthesised.
    content_W = W - 2 * bleed_px
    content_H = H - 2 * bleed_px
    crop = card_img.crop((
        bleed_px + int(content_W * 0.82),
        bleed_px + int(content_H * 0.945),
        bleed_px + int(content_W * 0.95),
        bleed_px + int(content_H * 0.977),
    ))
    arr = np.array(crop.convert("L"))
    # Binarise — text in the ID box is dark on a bright pill. Anything under
    # gray 80 becomes black, anything else white.
    binary = np.where(arr < 80, 0, 255).astype(np.uint8)
    # Upscale by 6x with nearest-neighbour: improves tesseract accuracy on
    # small text without smoothing strokes together.
    big = np.kron(binary, np.ones((6, 6), dtype=np.uint8))

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        Image.fromarray(big).save(tmp.name)
        try:
            result = subprocess.run(
                ["tesseract", tmp.name, "-", "--psm", "6",
                 "-c", "tessedit_char_whitelist=0123456789VBPA"],
                capture_output=True, text=True, timeout=10,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    text = result.stdout.strip()
    m = re.search(r"(\d{1,3})\s*(V|VP|BP|AP|B)\s*(\d{1,3})", text)
    if m:
        return m.group(1), m.group(2), int(m.group(3))
    return None


def ocr_set_prefix(card_paths: list[Path],
                   bleed_px: int = 0,
                   verbose: bool = False) -> Optional[tuple[str, str]]:
    """
    Determine the set prefix of a deck by OCRing several extracted cards and
    taking the majority vote.

    Returns (set_number_str, prefix_letters) — e.g. ("61", "V") — or None
    if no consensus could be reached.

    Pass bleed_px if the cards have a synthesized bleed area so the OCR
    region targets the actual content.

    Individual OCR reads of small card-ID text are unreliable (~40-60%
    failure rate on these cards), but the set prefix is the same on every
    card in the deck. Voting across many cards trades higher per-card
    failure for very high overall confidence.
    """
    from collections import Counter
    votes: Counter[tuple[str, str]] = Counter()
    for path in card_paths:
        try:
            img = Image.open(path)
        except OSError:
            continue
        r = ocr_card_id(img, bleed_px=bleed_px)
        if r:
            votes[(r[0], r[1])] += 1

    if not votes:
        if verbose:
            print(f"  OCR: no card IDs could be read from {len(card_paths)} cards")
        return None

    winner, count = votes.most_common(1)[0]
    if verbose:
        print(f"  OCR set-prefix vote: {dict(votes)} → {winner[0]}{winner[1]} "
              f"({count}/{sum(votes.values())} reads)")
    # Require at least 2 votes to commit (defends against a single noisy read).
    if count < 2:
        return None
    return winner


def _sample_border_colour(card: Image.Image) -> tuple[int, int, int]:
    """
    Sample the dominant colour of a rectangular card's outermost edge.

    Samples a thin strip from each of the four edges (mid 50% of the edge,
    to avoid any corner artefacts) and returns the median colour. Works for
    any solid-coloured border: black, dark grey, off-white, etc.

    For "borderless" community cards whose outermost pixels are a light
    grey card frame, this samples that frame colour — which means the
    bleed would extend the light grey rather than matching the more typical
    black. We recommend not using --bleed with borderless sets.
    """
    arr = np.array(card.convert("RGB"))
    H, W = arr.shape[:2]
    if H < 20 or W < 20:
        return (0, 0, 0)

    x_lo, x_hi = W // 4, 3 * W // 4
    y_lo, y_hi = H // 4, 3 * H // 4

    strips = [
        arr[0:4,   x_lo:x_hi],       # top edge
        arr[H-4:H, x_lo:x_hi],       # bottom edge
        arr[y_lo:y_hi, 0:4],         # left edge
        arr[y_lo:y_hi, W-4:W],       # right edge
    ]
    all_samples = np.concatenate([s.reshape(-1, 3) for s in strips], axis=0)
    # Median is robust to occasional non-border pixels (e.g. small icons
    # poking out to the frame edge).
    median = np.median(all_samples, axis=0)
    return (int(median[0]), int(median[1]), int(median[2]))


def synthesize_bleed(card: Image.Image, bleed_px: int,
                     overwrite_px: int = 0) -> Image.Image:
    """
    Add a synthesized bleed area around a rectangular card image.

    Two-step approach:
      1. Overwrite the outer `overwrite_px` pixels on each side of the card
         with the sampled border colour. This eats any anti-aliased rounded-
         corner artefacts and any thin off-colour fringe at the very edge of
         the PDF-rendered card.
      2. Extend the card outward by `bleed_px` pixels on each side, filling
         the new area with the same border colour.

    Result: a fully rectangular RGB image. The card content is preserved
    everywhere except in the outermost `overwrite_px` ring, which was just
    the card's own black/grey frame anyway.

    Both quantities should be sized for the rendering DPI:
        bleed_px      = 3 mm * dpi / 25.4   (the actual print bleed)
        overwrite_px  = 1 mm * dpi / 25.4   (cleans the existing edge)
    """
    if bleed_px <= 0 and overwrite_px <= 0:
        return card

    border_colour = _sample_border_colour(card)
    arr = np.array(card.convert("RGB"))
    H, W = arr.shape[:2]

    # Step 1: overwrite the existing outer ring of the card with the border
    # colour. This is unconditional — every card in this PDF family has a
    # solid border, so painting the outer mm with the sampled border colour
    # only obliterates pixels that were already that colour (plus any AA or
    # corner-curve junk that bleeds the PDF page background through).
    if overwrite_px > 0 and W > 2 * overwrite_px and H > 2 * overwrite_px:
        arr[:overwrite_px, :] = border_colour          # top strip
        arr[H - overwrite_px:, :] = border_colour      # bottom strip
        arr[:, :overwrite_px] = border_colour          # left strip
        arr[:, W - overwrite_px:] = border_colour      # right strip

    # Step 2: extend the card outward by bleed_px on each side.
    if bleed_px <= 0:
        return Image.fromarray(arr)

    canvas = Image.new("RGB", (W + 2 * bleed_px, H + 2 * bleed_px),
                       border_colour)
    canvas.paste(Image.fromarray(arr), (bleed_px, bleed_px))
    return canvas


def extract_card(img: Image.Image, bbox: tuple[int, int, int, int],
                 radius: int, rounded: bool,
                 snap_black: bool = False,
                 bleed_px: int = 0,
                 overwrite_px: int = 0) -> Image.Image:
    """Crop a card from the page image, optionally apply rounded-corner alpha,
    snap near-black pixels to pure black for clean print output, and add a
    synthesized bleed area around the result.

    Note: bleed and rounded corners are mutually exclusive. When bleed is
    applied the card is kept rectangular — MPC and similar print services
    cut rounded corners at the press, so a rectangular bleed-aware image
    with no alpha is the cleanest input.
    """
    card = img.crop(bbox)
    if snap_black:
        card = snap_near_black_to_pure_black(card)
    if bleed_px > 0:
        # Print-bleed path: keep card rectangular, overwrite the outer
        # `overwrite_px` of card edge with the sampled border colour to
        # clean up any rounded-corner / AA fringe, then extend outward.
        return synthesize_bleed(card, bleed_px, overwrite_px=overwrite_px)
    if not rounded:
        return card
    # Display path: apply rounded-corner alpha for nicer on-screen viewing.
    card_rgba = card.convert("RGBA")
    mask = Image.new("L", card.size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle(
        (0, 0, card.size[0] - 1, card.size[1] - 1),
        radius=radius, fill=255,
    )
    card_rgba.putalpha(mask)
    return card_rgba


def process_page(pdf_path: Path, page_num: int, dpi: int,
                 rounded: bool, snap_black: bool,
                 bleed_px: int, overwrite_px: int,
                 id_sequence_prefix: Optional[str],
                 id_sequence_next: Optional[int],
                 out_dir: Path, tmpdir: Path,
                 verbose: bool) -> tuple[list[Path], Optional[int]]:
    """Process one page: render, detect grid, parse text, extract cards.

    If id_sequence_prefix is set, cards on this page are named using
    {prefix}{n:02d} starting from id_sequence_next, in reading order
    (left-to-right, top-to-bottom). Returns the updated next-number along
    with the list of saved paths.
    """
    if verbose:
        print(f"\nPage {page_num}:")

    page_img_path = render_page(pdf_path, page_num, dpi, tmpdir)
    img = Image.open(page_img_path)
    arr = np.array(img.convert("L"))

    geom = detect_page_geometry(arr, verbose=verbose)
    n_cells = geom.n_rows * geom.n_cols

    words = get_page_words(pdf_path, page_num)
    metas = parse_card_metadata_by_position(words, geom, arr.shape[1], arr.shape[0])

    if verbose:
        found_ids = sum(1 for m in metas if m and m.card_id)
        found_titles = sum(1 for m in metas if m and m.title)
        print(f"    metadata: {found_ids}/{n_cells} ids, {found_titles}/{n_cells} titles")

    # Decide whether this page actually contains a card grid. Some PDFs include
    # cover sheets, tables of contents, or other non-card pages we should skip.
    #
    # Signals that this is NOT a card page:
    #   - cells in the detected grid have a wildly wrong aspect ratio (real
    #     trading cards are ~0.7 wide/tall; >1.3 means we found a header band);
    #   - no card IDs found AND cells aren't clearly portrait-shaped.
    grid_w = geom.x_edges[-1] - geom.x_edges[0]
    grid_h = geom.y_edges[-1] - geom.y_edges[0]
    cell_w = grid_w / geom.n_cols
    cell_h = grid_h / geom.n_rows
    aspect = cell_w / cell_h if cell_h > 0 else float("inf")
    n_ids = sum(1 for m in metas if m and m.card_id)
    if aspect > 1.3 or (n_ids == 0 and aspect > 1.0 and id_sequence_prefix is None):
        if verbose:
            print(f"    skipping: doesn't look like a card grid "
                  f"(cell aspect {aspect:.2f}, {n_ids} card IDs found)")
        return [], id_sequence_next

    # Detect which grid cells actually contain a card.
    # A cell is "empty" if its mean brightness > 245 (mostly white background).
    cells_with_cards: list[tuple[int, int]] = []
    for row in range(geom.n_rows):
        for col in range(geom.n_cols):
            L, T, R, B = geom.cell(row, col)
            cell_arr = arr[T:B, L:R]
            if cell_arr.size and cell_arr.mean() < 245:
                cells_with_cards.append((row, col))

    if verbose and len(cells_with_cards) < n_cells:
        print(f"    only {len(cells_with_cards)} cells contain cards "
              f"(rest are blank)")

    saved: list[Path] = []
    next_n = id_sequence_next
    for row, col in cells_with_cards:
        bbox = geom.cell(row, col)
        card_img = extract_card(img, bbox, geom.corner_radius, rounded,
                                snap_black=snap_black,
                                bleed_px=bleed_px,
                                overwrite_px=overwrite_px)

        meta = metas[row * geom.n_cols + col]

        # Decide stem. --id-sequence overrides any detected ID/title:
        # synthesise an ID PREFIX{N:02d} for every card in reading order.
        if id_sequence_prefix is not None and next_n is not None:
            stem = f"{id_sequence_prefix}{next_n:02d}"
            next_n += 1
        elif meta and meta.card_id and meta.title:
            stem = f"{meta.set_prefix}{meta.number:02d}_{slugify(meta.title)}"
        elif meta and meta.card_id:
            stem = f"{meta.set_prefix}{meta.number:02d}"
        elif meta and meta.title:
            stem = f"page{page_num:02d}_r{row+1}c{col+1}_{slugify(meta.title)}"
        else:
            stem = f"page{page_num:02d}_r{row+1}c{col+1}"

        out_path = out_dir / f"{stem}.png"
        card_img.save(out_path)
        saved.append(out_path)
        if verbose:
            id_str = meta.card_id if meta and meta.card_id else "—"
            title_str = meta.title if meta and meta.title else "—"
            print(f"    [{row},{col}] {id_str:>6}  '{title_str}'  -> {out_path.name}")

    return saved, next_n


def process_pdf(pdf_path: Path, *,
                out_dir: Path,
                dpi: int,
                rounded: bool,
                snap_black: bool,
                bleed_mm: float,
                id_sequence: Optional[tuple[str, int]],
                pages_spec: Optional[str],
                verbose: bool) -> tuple[int, list[str]]:
    """
    Process a single PDF: render pages, extract cards, write outputs.

    Returns (n_cards_saved, error_messages). The function captures per-page
    errors so a problem with one page doesn't abort the rest of the file —
    important when batch-processing a directory.

    If id_sequence is provided as (prefix, start_number), every extracted
    card across all pages gets a synthetic ID PREFIX{N:02d} in reading order,
    starting at start_number. Use this for raster-only PDFs where pdftotext
    extracts no text.
    """
    errors: list[str] = []
    out_dir.mkdir(parents=True, exist_ok=True)

    n_pages = get_page_count(pdf_path)
    if pages_spec:
        m = re.match(r"^(\d+)(?:-(\d+))?$", pages_spec.strip())
        if not m:
            errors.append(f"--pages spec '{pages_spec}' must be like '1' or '2-5'")
            return 0, errors
        first = int(m.group(1))
        last = int(m.group(2)) if m.group(2) else first
    else:
        first = 1
        last = n_pages if n_pages > 0 else 100  # try until we fail

    # Running ID counter (only used if id_sequence is set).
    next_id_number = id_sequence[1] if id_sequence else None

    # Convert the requested bleed from mm to pixels at the current DPI.
    # 1 inch = 25.4 mm; bleed_px = bleed_mm * dpi / 25.4.
    bleed_px = int(round(bleed_mm * dpi / 25.4)) if bleed_mm > 0 else 0
    # Companion overwrite of the outermost 1mm of card edge with the sampled
    # border colour. This cleans up any anti-aliased rounded-corner pixels
    # that would otherwise show as a thin halo against the new bleed. Only
    # applied when bleed is requested.
    overwrite_px = int(round(1.0 * dpi / 25.4)) if bleed_px > 0 else 0
    if verbose and bleed_px > 0:
        print(f"  bleed: {bleed_mm}mm = {bleed_px}px at {dpi} DPI "
              f"(plus 1mm = {overwrite_px}px edge overwrite)")

    n_saved = 0
    pages_processed = 0
    all_saved: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="extract_cards_") as td:
        tmpdir = Path(td)
        for page_num in range(first, last + 1):
            try:
                saved, next_id_number = process_page(
                    pdf_path, page_num, dpi,
                    rounded=rounded,
                    snap_black=snap_black,
                    bleed_px=bleed_px,
                    overwrite_px=overwrite_px,
                    id_sequence_prefix=id_sequence[0] if id_sequence else None,
                    id_sequence_next=next_id_number,
                    out_dir=out_dir, tmpdir=tmpdir,
                    verbose=verbose,
                )
                n_saved += len(saved)
                all_saved.extend(saved)
                pages_processed += 1
            except subprocess.CalledProcessError as e:
                # pdftoppm fails when we've gone past the last page — that's
                # expected. But if it fails on the very first page we tried,
                # the PDF is genuinely unreadable, not just exhausted.
                if pages_spec is None and pages_processed > 0:
                    if verbose:
                        print(f"  page {page_num}: not found (end of document)")
                    break
                # Trim the verbose subprocess message to just the exit info.
                errors.append(
                    f"page {page_num}: pdftoppm failed "
                    f"(exit {e.returncode}) — PDF may be unreadable"
                )
                # Don't keep trying further pages of an unreadable file.
                break
            except Exception as e:
                errors.append(f"page {page_num}: {e.__class__.__name__}: {e}")
                if verbose:
                    import traceback
                    traceback.print_exc()

    # OCR fallback: if any cards ended up with provisional 'pageNN_rNcN' names
    # (which happens when the PDF has no extractable text and no --id-sequence
    # was provided), try to determine the set prefix by OCRing several cards
    # and voting. The cards in a set all share the same prefix, so even with
    # noisy individual reads we usually get a clean consensus.
    unnamed_pattern = re.compile(r"^page\d{2}_r\d+c\d+")
    unnamed = [p for p in all_saved if unnamed_pattern.match(p.stem)]
    if unnamed and id_sequence is None:
        if verbose:
            print(f"  no card IDs from text; trying OCR fallback "
                  f"on {len(unnamed)} unnamed card(s)")
        if not _tesseract_available():
            errors.append(
                f"{len(unnamed)} card(s) couldn't be named (no PDF text "
                f"and tesseract not available; install with: brew install tesseract)"
            )
        else:
            prefix = ocr_set_prefix(unnamed, bleed_px=bleed_px,
                                    verbose=verbose)
            if prefix is None:
                errors.append(
                    f"{len(unnamed)} card(s) couldn't be named (no PDF text "
                    f"and OCR couldn't determine the set prefix)"
                )
            else:
                # Rename in reading order. all_saved is in extraction order
                # (page 1 r1c1, r1c2, …), which matches the printed card
                # numbers in every STCCG-style PDF we've seen.
                set_num, set_letters = prefix
                renamed = 0
                for n, path in enumerate(unnamed, start=1):
                    new_stem = f"{set_num}{set_letters}{n:02d}"
                    new_path = path.with_name(f"{new_stem}.png")
                    try:
                        path.rename(new_path)
                        renamed += 1
                    except OSError as e:
                        errors.append(f"rename {path.name} → {new_path.name}: {e}")
                if verbose:
                    print(f"  renamed {renamed} card(s) using OCR'd prefix "
                          f"{set_num}{set_letters}")

    # If nothing came out and we have no explicit error, surface that fact.
    if n_saved == 0 and not errors:
        errors.append("no cards extracted (PDF may be empty or malformed)")

    return n_saved, errors


def main() -> int:
    p = argparse.ArgumentParser(
        description="Extract individual cards from 9-cards-per-page PDF(s). "
                    "Accepts either a single PDF or a directory of PDFs.",
    )
    p.add_argument("input", type=Path,
                   help="Path to a PDF file, or a directory containing PDFs.")
    p.add_argument("--out", type=Path, default=None,
                   help="Output directory. For a single PDF, defaults to "
                        "<pdfstem>_cards/ next to the input. For a directory "
                        "of PDFs, defaults to <input>/extracted/.")
    p.add_argument("--per-pdf-subdir", action="store_true",
                   help="When input is a directory, write each PDF into its "
                        "own sub-folder under the output root.")
    p.add_argument("--dpi", type=int, default=300,
                   help="Render DPI (default: 300).")
    p.add_argument("--no-round", action="store_true",
                   help="Don't apply rounded-corner transparency; "
                        "keep rectangular crops.")
    p.add_argument("--snap-black", action="store_true",
                   help="Snap near-neutral very-dark pixels to pure RGB (0,0,0). "
                        "Recommended when preparing cards for CMYK on-demand "
                        "printing (e.g. MakePlayingCards) to keep small text "
                        "K-only and avoid registration fringing. Leaves "
                        "anti-aliased edges and dark coloured art untouched.")
    p.add_argument("--bleed", type=float, nargs="?", const=3.0, default=0.0,
                   metavar="MM",
                   help="Synthesize a bleed area around each card by extending "
                        "the border colour outward. Required for most on-demand "
                        "print services (MPC etc.). Pass '--bleed' for the "
                        "default 3mm, or '--bleed N' for a custom amount in mm. "
                        "Cards are output rectangular (rounded corners are "
                        "trimmed at the press). NOTE: not recommended for "
                        "'borderless' card sets — the sampled border colour "
                        "would be the light frame, not black.")
    p.add_argument("--id-sequence", type=str, default=None,
                   help="Force sequential card IDs when the PDF has no "
                        "extractable text. Format: 'PREFIX:START', e.g. "
                        "'53V:1' produces 53V01, 53V02, ... in reading order "
                        "across pages. Applied to every PDF unless a sidecar "
                        "file overrides it (see below). "
                        "Per-PDF override: create '<pdfstem>.id' next to the "
                        "PDF containing the same 'PREFIX:START' format. "
                        "Lets you mix text-extractable PDFs and raster-only "
                        "PDFs in one batch.")
    p.add_argument("--pages", type=str, default=None,
                   help="Page range, e.g. '1-3' or '2'. Applies to every "
                        "input PDF. Default: all pages.")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Print measurements and per-card info.")
    args = p.parse_args()

    if not args.input.exists():
        sys.exit(f"Input not found: {args.input}")

    check_tools()

    # Parse the --id-sequence flag once
    id_sequence: Optional[tuple[str, int]] = None
    if args.id_sequence:
        m = re.match(r"^([A-Z0-9]+):(\d+)$", args.id_sequence)
        if not m:
            sys.exit("--id-sequence must be like 'PREFIX:START', e.g. '53V:1'")
        id_sequence = (m.group(1), int(m.group(2)))

    # Build the list of PDFs to process
    if args.input.is_dir():
        pdfs = sorted(
            p for p in args.input.iterdir()
            if p.is_file() and p.suffix.lower() == ".pdf"
        )
        if not pdfs:
            sys.exit(f"No PDF files found in directory: {args.input}")
        default_out_root = args.input / "extracted"
        per_pdf_subdir = args.per_pdf_subdir
    else:
        if args.input.suffix.lower() != ".pdf":
            sys.exit(f"Input file is not a PDF: {args.input}")
        pdfs = [args.input]
        default_out_root = args.input.parent / f"{args.input.stem}_cards"
        per_pdf_subdir = False

    out_root = args.out or default_out_root

    # Process each PDF, collecting per-file results.
    Result = tuple[Path, int, list[str]]  # (pdf_path, n_saved, errors)
    results: list[Result] = []
    total_pdfs = len(pdfs)

    for i, pdf in enumerate(pdfs, start=1):
        # Header line — always printed so the user sees progress in batch mode.
        prefix = f"[{i}/{total_pdfs}] " if total_pdfs > 1 else ""
        print(f"{prefix}{pdf.name}")

        if per_pdf_subdir:
            pdf_out_dir = out_root / pdf.stem
        else:
            pdf_out_dir = out_root

        # Per-PDF id sequence: command-line --id-sequence applies globally,
        # but a sidecar file '<pdfstem>.id' next to the PDF overrides it on a
        # per-PDF basis. This lets you batch-process a directory where most
        # PDFs have extractable text (no sidecar needed) and a few are
        # raster-only (drop in a 'MyRasterSet.id' containing 'PREFIX:START').
        pdf_id_sequence = id_sequence
        sidecar = pdf.with_suffix(".id")
        if sidecar.exists():
            try:
                content = sidecar.read_text().strip()
                m = re.match(r"^([A-Z0-9]+):(\d+)$", content)
                if m:
                    pdf_id_sequence = (m.group(1), int(m.group(2)))
                    if args.verbose:
                        print(f"  using sidecar {sidecar.name}: "
                              f"{m.group(1)}:{m.group(2)}")
                else:
                    print(f"  ! sidecar {sidecar.name} is malformed "
                          f"(expected 'PREFIX:START', got {content!r})")
            except OSError as e:
                print(f"  ! couldn't read sidecar {sidecar.name}: {e}")

        try:
            n_saved, errors = process_pdf(
                pdf,
                out_dir=pdf_out_dir,
                dpi=args.dpi,
                rounded=not args.no_round,
                snap_black=args.snap_black,
                bleed_mm=args.bleed,
                id_sequence=pdf_id_sequence,
                pages_spec=args.pages,
                verbose=args.verbose,
            )
        except Exception as e:
            # Catch-all so a single bad file doesn't abort the whole batch.
            n_saved = 0
            errors = [f"{e.__class__.__name__}: {e}"]
            if args.verbose:
                import traceback
                traceback.print_exc()

        results.append((pdf, n_saved, errors))

        # Per-PDF summary line
        if errors:
            err_summary = f", {len(errors)} error(s)"
        else:
            err_summary = ""
        print(f"  -> {n_saved} cards to {pdf_out_dir}{err_summary}")
        for err in errors:
            print(f"     ! {err}")

    # Batch summary (only useful when there's more than one PDF)
    if total_pdfs > 1:
        total_cards = sum(n for _, n, _ in results)
        n_clean = sum(1 for _, _, e in results if not e)
        n_with_errors = total_pdfs - n_clean
        print()
        print(f"Done: {total_cards} cards from {total_pdfs} PDF(s); "
              f"{n_clean} clean, {n_with_errors} with errors.")
        if n_with_errors:
            print("PDFs with errors:")
            for pdf, _, errs in results:
                if errs:
                    print(f"  {pdf.name}: {len(errs)} error(s)")
            return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
