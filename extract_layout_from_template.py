#!/usr/bin/env python3
"""
PSD Template Extractor for Star Trek CCG 2E HD Card Templates

Reads a faction PSD template and produces:
  1. A JSON specification of every layer (position, type, content, styling)
  2. A folder of extracted PNG assets for every pixel layer (borders, icons, etc.)
  3. A flat list of all fonts referenced, so they can be installed

The resulting JSON + assets folder is everything a downstream renderer needs to
produce cards without ever touching the PSD again.

Usage:
    python extract_psd.py <path_to_psd> <output_dir>

Example:
    python extract_psd.py "2e HD Federation v1.psd" ./extracted/federation
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from psd_tools import PSDImage
from psd_tools.api.layers import Group, PixelLayer, TypeLayer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def read_psd_dpi(psd_path: Path) -> tuple[float, float]:
    """psd-tools doesn't expose ResolutionInfo cleanly, so parse it directly."""
    data = psd_path.read_bytes()
    idx = 0
    while idx < len(data) - 12:
        if data[idx:idx + 4] == b'8BIM':
            resource_id = struct.unpack('>H', data[idx + 4:idx + 6])[0]
            if resource_id == 1005:  # ResolutionInfo
                name_len = data[idx + 6]
                name_end = idx + 7 + name_len
                if name_end % 2 == 1:
                    name_end += 1
                size = struct.unpack('>I', data[name_end:name_end + 4])[0]
                res = data[name_end + 4:name_end + 4 + size]
                h_dpi = struct.unpack('>I', res[0:4])[0] / 65536.0
                v_dpi = struct.unpack('>I', res[8:12])[0] / 65536.0
                return h_dpi, v_dpi
        idx += 1
    return 72.0, 72.0


def sanitize_filename(name: str) -> str:
    """Make a layer name safe for filesystem use."""
    keep = "-_.() "
    cleaned = "".join(c if (c.isalnum() or c in keep) else "_" for c in name)
    return cleaned.strip().replace(" ", "_") or "unnamed"


def fill_color_to_rgba(fill_color: dict | None) -> list[int] | None:
    """Convert PSD FillColor dict to 8-bit RGBA list.
    PSD stores Type 1 = RGB with floats in [0,1] as [R, G, B, A]."""
    if not fill_color:
        return None
    values = fill_color.get('Values', [])
    if len(values) < 4:
        return None
    # PSD order is [A, R, G, B] when Type==1; verify by inspection
    # The earlier dump showed FillColor: {'Type': 1, 'Values': [1.0, 1.0, 1.0, 1.0]}
    # for white text and [1.0, 0.0, 0.00038, 0.0004] for black — so order is
    # [A, R, G, B], meaning the second value is R. The "white" entry happens to be
    # all 1s so it's ambiguous, but the black entry is unambiguous: alpha=1, R=0.
    a, r, g, b = values[0], values[1], values[2], values[3]
    return [int(round(r * 255)), int(round(g * 255)), int(round(b * 255)), int(round(a * 255))]


JUSTIFICATION_MAP = {0: "left", 1: "right", 2: "center", 3: "justify_last_left",
                     4: "justify_last_right", 5: "justify_last_center", 6: "justify_all"}
FONT_CAPS_MAP = {0: "normal", 1: "small_caps", 2: "all_caps"}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class LayerSpec:
    name: str
    path: str                   # "Group > SubGroup > Layer"
    kind: str                   # "group" | "pixel" | "text"
    visible: bool
    bbox: list[int] | None      # [left, top, right, bottom] in PSD pixels
    size: list[int] | None      # [width, height]

    # Text-only fields
    text: str | None = None
    font: str | None = None
    font_size: float | None = None
    color: list[int] | None = None
    justification: str | None = None
    font_caps: str | None = None
    faux_bold: bool | None = None
    faux_italic: bool | None = None
    tracking: int | None = None
    leading: float | None = None

    # Pixel-only fields
    asset_file: str | None = None  # relative path to extracted PNG

    # Group-only fields
    children: list["LayerSpec"] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

class Extractor:
    def __init__(self, psd_path: Path, output_dir: Path):
        self.psd_path = psd_path
        self.output_dir = output_dir
        self.assets_dir = output_dir / "assets"
        self.psd = PSDImage.open(psd_path)
        self.fonts_used: set[str] = set()

    def run(self) -> dict[str, Any]:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.assets_dir.mkdir(parents=True, exist_ok=True)

        h_dpi, v_dpi = read_psd_dpi(self.psd_path)

        # Walk top-level layers
        root_layers = [self._extract_layer(layer, parent_path="") for layer in self.psd]

        spec = {
            "source_psd": self.psd_path.name,
            "canvas": {
                "width": self.psd.width,
                "height": self.psd.height,
                "dpi": [h_dpi, v_dpi],
                "size_inches": [self.psd.width / h_dpi, self.psd.height / v_dpi],
            },
            "fonts_required": sorted(self.fonts_used),
            "layers": [self._dataclass_to_dict(l) for l in root_layers],
        }

        # Write the JSON
        json_path = self.output_dir / "spec.json"
        json_path.write_text(json.dumps(spec, indent=2))

        # Also render a flat preview of the template (intro hidden)
        self._render_preview()

        return spec

    def _extract_layer(self, layer, parent_path: str) -> LayerSpec:
        layer_path = f"{parent_path} > {layer.name}" if parent_path else layer.name

        # Bounding box — may be (0,0,0,0) for empty groups
        try:
            bbox = [layer.left, layer.top, layer.right, layer.bottom]
            size = [layer.width, layer.height]
            if size == [0, 0]:
                bbox = None
                size = None
        except Exception:
            bbox = None
            size = None

        spec = LayerSpec(
            name=layer.name,
            path=layer_path,
            kind="unknown",
            visible=bool(layer.visible),
            bbox=bbox,
            size=size,
        )

        # Groups recurse
        if isinstance(layer, Group):
            spec.kind = "group"
            for child in layer:
                spec.children.append(self._extract_layer(child, layer_path))
            return spec

        # Text layers
        if isinstance(layer, TypeLayer):
            spec.kind = "text"
            spec.text = layer.text
            self._extract_text_style(layer, spec)
            return spec

        # Everything else: treat as pixel layer and export as PNG
        if isinstance(layer, PixelLayer) or hasattr(layer, 'composite'):
            spec.kind = "pixel"
            if size:  # don't try to export 0x0 layers
                asset_rel = self._export_pixel_asset(layer, layer_path)
                spec.asset_file = asset_rel
            return spec

        return spec

    def _extract_text_style(self, layer: TypeLayer, spec: LayerSpec) -> None:
        """Pull font, size, color, justification etc. from the first style run."""
        try:
            ed = layer.engine_dict
            rd = layer.resource_dict
        except Exception:
            return

        # Fonts: from document-level resource dict, indexed by Font field
        font_set = rd.get('FontSet', [])
        font_names: list[str] = []
        for f in font_set:
            try:
                name_obj = f.get('Name', '')
                # psd-tools wraps strings in a String class whose str() adds quotes;
                # use .value to get the raw Python str.
                name_str = name_obj.value if hasattr(name_obj, 'value') else str(name_obj)
                font_names.append(name_str)
            except Exception:
                font_names.append('')

        # First style run (most cards have homogeneous styling per layer)
        style_runs = ed.get('StyleRun', {})
        run_array = style_runs.get('RunArray', [])
        if run_array:
            ssd = run_array[0].get('StyleSheet', {}).get('StyleSheetData', {})
            # psd-tools wraps ints in Integer (not a subclass of int); coerce via int()
            font_idx_raw = ssd.get('Font', 0)
            try:
                font_idx = int(font_idx_raw)
            except (TypeError, ValueError):
                font_idx = -1
            if 0 <= font_idx < len(font_names):
                spec.font = font_names[font_idx]
                if spec.font and spec.font != 'AdobeInvisFont':
                    self.fonts_used.add(spec.font)
            fs_raw = ssd.get('FontSize', 0)
            try:
                spec.font_size = float(fs_raw) or None
            except (TypeError, ValueError):
                spec.font_size = None
            spec.color = fill_color_to_rgba(ssd.get('FillColor'))
            spec.font_caps = FONT_CAPS_MAP.get(int(ssd.get('FontCaps', 0)))
            spec.faux_bold = bool(ssd.get('FauxBold', False))
            spec.faux_italic = bool(ssd.get('FauxItalic', False))
            try:
                spec.tracking = int(ssd.get('Tracking', 0))
            except (TypeError, ValueError):
                spec.tracking = 0
            leading = ssd.get('Leading', 0.0)
            try:
                spec.leading = float(leading) if leading else None
            except (TypeError, ValueError):
                spec.leading = None

        # Paragraph alignment
        para = ed.get('ParagraphRun', {}).get('RunArray', [])
        if para:
            props = para[0].get('ParagraphSheet', {}).get('Properties', {})
            try:
                just_idx = int(props.get('Justification', 0))
            except (TypeError, ValueError):
                just_idx = 0
            spec.justification = JUSTIFICATION_MAP.get(just_idx)

    def _export_pixel_asset(self, layer, layer_path: str) -> str | None:
        """Composite this pixel layer to a PNG with transparency."""
        try:
            img = layer.composite()
        except Exception as e:
            print(f"  ! could not composite {layer_path}: {e}", file=sys.stderr)
            return None
        if img is None:
            return None

        # Build a filesystem-safe relative path mirroring the layer tree
        parts = [sanitize_filename(p) for p in layer_path.split(" > ")]
        rel_path = Path(*parts).with_suffix(".png")
        full_path = self.assets_dir / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(full_path)
        return str(Path("assets") / rel_path)

    def _render_preview(self) -> None:
        """Render the full template with the intro overlay hidden (if present)."""
        # Re-open so we don't mutate the parsed tree used above
        psd = PSDImage.open(self.psd_path)
        for layer in psd:
            if layer.name.lower() == 'intro':
                layer.visible = False
            if layer.name == 'Card Image':
                for child in layer:
                    if 'Place Image' in child.name:
                        child.visible = False
        try:
            preview = psd.composite(force=True)
            preview.save(self.output_dir / "preview.png")
        except Exception as e:
            print(f"  ! could not render preview: {e}", file=sys.stderr)

    def _dataclass_to_dict(self, spec: LayerSpec) -> dict:
        """Convert LayerSpec to dict, recursing into children, dropping None fields."""
        d = asdict(spec)
        # Drop None/empty fields for readability
        cleaned = {k: v for k, v in d.items() if v not in (None, [], "")}
        if spec.children:
            cleaned['children'] = [self._dataclass_to_dict(c) for c in spec.children]
        elif 'children' in cleaned:
            del cleaned['children']
        return cleaned


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Extract a Star Trek CCG 2E HD PSD template.")
    ap.add_argument("psd", type=Path, help="Path to the .psd file")
    ap.add_argument("output_dir", type=Path, help="Directory to write spec + assets")
    args = ap.parse_args()

    if not args.psd.exists():
        sys.exit(f"PSD not found: {args.psd}")

    print(f"Extracting {args.psd.name} -> {args.output_dir}")
    extractor = Extractor(args.psd, args.output_dir)
    spec = extractor.run()

    # Summary
    print(f"\nCanvas: {spec['canvas']['width']}x{spec['canvas']['height']} "
          f"@ {spec['canvas']['dpi'][0]} DPI "
          f"({spec['canvas']['size_inches'][0]:.2f}\" x {spec['canvas']['size_inches'][1]:.2f}\")")
    print(f"\nFonts required ({len(spec['fonts_required'])}):")
    for f in spec['fonts_required']:
        print(f"  - {f}")

    def count(layers, k):
        n = 0
        for l in layers:
            if l.get('kind') == k:
                n += 1
            n += count(l.get('children', []), k)
        return n
    print(f"\nLayer counts:")
    for k in ('group', 'pixel', 'text'):
        print(f"  {k:6s}: {count(spec['layers'], k)}")

    print(f"\nWrote:")
    print(f"  {args.output_dir}/spec.json")
    print(f"  {args.output_dir}/preview.png")
    print(f"  {args.output_dir}/assets/  ({sum(1 for _ in (args.output_dir / 'assets').rglob('*.png'))} PNG assets)")


if __name__ == "__main__":
    main()
