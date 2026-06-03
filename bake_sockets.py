"""
Photopea-driven socket extractor for the affiliation templates.

Each affiliation PSD ships its Slot 1 socket (chrome disc + up/down connector
tabs) as a vector mask with PS layer effects (color overlay + stroke). psd-tools
and psd2svg both return zero-alpha for those layers because they don't simulate
Photoshop layer effects. Photopea does — and it accepts scripts + binary PSD
via its postMessage iframe API. We drive a headless Chromium via Playwright to:

    1. Open the PSD,
    2. Isolate the Slot 1/Base layer,
    3. Export the full canvas as PNG,
    4. Trim to the layer's non-transparent bbox + alpha-taper the rightmost N
       columns (so the socket blends back into the chrome strip when pasted,
       mirroring extract_socket.py's finish).

Federation already has Card_Background/Icon_Socket.png from extract_socket.py
(cropped from Layer_11's baked slot 2 socket) — left alone.

Future work (intentionally not done here):
- Bake each affiliation's actual icon glyphs (Cmd, Stf, era affiliations
  Maquis/TNG/etc., time-period AU/Future/Past) via the same pipeline. We
  currently reuse Federation's glyphs as a stand-in — likely fine for Cmd/Stf
  (universal symbols), possibly tinted differently per affiliation for the era
  icons.

Run:
    python3 bake_sockets.py
"""

import asyncio
import base64
import io
import json
from pathlib import Path

import numpy as np
from PIL import Image
from playwright.async_api import async_playwright


# (display name, PSD filename in templates/, output subdir under extracted/)
TARGETS = [
    ("Non-Aligned", "2e HD Non-Alligned v1.psd", "nonaligned"),
    ("Bajoran",     "2e_HD_Bajoran_v1.psd",      "bajoran"),
    ("Cardassian",  "2e_HD_Cardassian_v1.psd",   "cardassian"),
    ("Dominion",    "2e_HD_Dominion_v1.psd",     "dominion"),
    ("Ferengi",     "2e HD Ferengi v1.psd",      "ferengi"),
    ("Klingon",     "2e HD Klingon v1.psd",      "klingon"),
    ("Romulan",     "2e HD Romulan v1.psd",      "romulan"),
    ("Borg",        "2e HD Borg v1.psd",         "borg"),
    ("Starfleet",   "2e HD Starfleet v1.psd",    "starfleet"),
    ("Vidiian",     "2e HD Vidiian v1.psd",      "vidiian"),
]

# Try in order until one resolves. NA + Ferengi + Klingon + Romulan use the
# first form (with the Personnel/Staffing intermediate); Bajoran, Cardassian,
# Dominion put the slot bases directly under Icons.
SOCKET_LAYER_PATHS = [
    ["Staffing and Attributes", "Personnel", "Staffing", "Slot 1", "Base"],
    ["Staffing and Attributes", "Icons", "Slot 1", "Base"],
]
TAPER_W = 5

TEMPLATES = Path("templates")
EXTRACTED = Path("extracted")

EMBED_HTML = """<!DOCTYPE html><html><body>
<iframe id="pp" src="https://www.photopea.com/?app=1" style="width:100%;height:100vh;border:0"></iframe>
<script>
window._mm = [];
window._bins = [];
window.addEventListener('message', e => {
    if (typeof e.data === 'string') window._mm.push(e.data);
    else if (e.data && e.data.byteLength !== undefined) {
        window._bins.push(e.data);
        window._mm.push('<bin#' + (window._bins.length - 1) + ' ' + e.data.byteLength + 'B>');
    } else window._mm.push('<obj>');
});
window.ppSendText = (s) => document.getElementById('pp').contentWindow.postMessage(s, '*');
window.ppSendBin = (b64) => {
    const bin = Uint8Array.from(atob(b64), c => c.charCodeAt(0)).buffer;
    document.getElementById('pp').contentWindow.postMessage(bin, '*');
};
window.getBinAsB64 = (idx) => {
    const b = window._bins[idx];
    let s = ''; const a = new Uint8Array(b);
    for (let i = 0; i < a.length; i++) s += String.fromCharCode(a[i]);
    return btoa(s);
};
window.clearBins = () => { window._bins = []; window._mm = []; };
</script></body></html>"""


async def wait_done(page, max_seconds=60):
    start = await page.evaluate("window._mm.length")
    for _ in range(max_seconds * 5):
        await asyncio.sleep(0.2)
        msgs = await page.evaluate(f"window._mm.slice({start})")
        if "done" in msgs:
            return msgs
    return await page.evaluate(f"window._mm.slice({start})")


async def run_script(page, js, max_seconds=30):
    await page.evaluate(f"window.ppSendText({json.dumps(js)})")
    return await wait_done(page, max_seconds)


def trim_and_taper(png_bytes: bytes) -> Image.Image:
    im = Image.open(io.BytesIO(png_bytes)).convert("RGBA")
    arr = np.array(im)
    a = arr[:, :, 3]
    ys, xs = np.where(a > 5)
    if len(ys) == 0:
        return im
    x0, y0, x1, y1 = xs.min(), ys.min(), xs.max() + 1, ys.max() + 1
    crop = arr[y0:y1, x0:x1].copy()
    alpha = crop[:, :, 3].astype(np.float32)
    w = crop.shape[1]
    for i in range(min(TAPER_W, w)):
        col = w - TAPER_W + i
        alpha[:, col] *= 1.0 - (i + 1) / (TAPER_W + 1)
    crop[:, :, 3] = alpha.astype(np.uint8)
    return Image.fromarray(crop)


async def bake_socket(page, psd_path: Path, out_dir: Path) -> tuple[bool, str]:
    psd_b64 = base64.b64encode(psd_path.read_bytes()).decode()
    await page.evaluate("window.clearBins()")
    await page.evaluate(f"window.ppSendBin('{psd_b64}')")
    await wait_done(page, 60)

    # The bake script tries each candidate path in order, reports back which
    # one matched (or "NONE"), and saves the PNG only when one matched.
    paths_js = json.dumps(SOCKET_LAYER_PATHS)
    bake_js = f"""
        var doc = app.activeDocument;
        var candidates = {paths_js};
        function hideAll(layers) {{
            for (var i = 0; i < layers.length; i++) {{
                layers[i].visible = false;
                if (layers[i].layers) hideAll(layers[i].layers);
            }}
        }}
        function showPath(layers, parts) {{
            for (var i = 0; i < layers.length; i++) {{
                if (layers[i].name === parts[0]) {{
                    layers[i].visible = true;
                    if (parts.length === 1) return true;
                    return showPath(layers[i].layers, parts.slice(1));
                }}
            }}
            return false;
        }}
        var matched = null;
        for (var c = 0; c < candidates.length; c++) {{
            hideAll(doc.layers);
            if (showPath(doc.layers, candidates[c])) {{
                matched = candidates[c].join('/');
                break;
            }}
        }}
        app.echoToOE(matched ? matched : 'NONE');
        if (matched) doc.saveToOE("png");
    """
    n_before = await page.evaluate("window._bins.length")
    msgs = await run_script(page, bake_js, max_seconds=45)
    matched_path = next((m for m in msgs if m not in ("done", "") and "<bin" not in m), "")
    if matched_path == "NONE" or not matched_path:
        await run_script(page, "app.activeDocument.close(false);", max_seconds=10)
        return False, f"no known layer path matched (msgs: {msgs[-3:]})"
    n_after = await page.evaluate("window._bins.length")
    if n_after <= n_before:
        await run_script(page, "app.activeDocument.close(false);", max_seconds=10)
        return False, f"no png returned (matched={matched_path})"

    b64 = await page.evaluate(f"window.getBinAsB64({n_after - 1})")
    im = trim_and_taper(base64.b64decode(b64))
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "Icon_Socket.png"
    im.save(out_path)
    await run_script(page, "app.activeDocument.close(false);", max_seconds=10)
    return True, f"{im.size} via {matched_path}"


async def main():
    Path("/tmp/pp_embed.html").write_text(EMBED_HTML)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--use-gl=swiftshader"])
        page = await browser.new_page()
        await page.goto("file:///tmp/pp_embed.html", wait_until="domcontentloaded")
        for _ in range(60):
            await asyncio.sleep(0.5)
            if "done" in await page.evaluate("window._mm.slice()"):
                break
        print("Photopea ready.")
        for affil_name, psd_file, outdir_name in TARGETS:
            psd_path = TEMPLATES / psd_file
            if not psd_path.exists():
                print(f"  ! missing: {psd_path}")
                continue
            out_dir = EXTRACTED / outdir_name / "assets" / "Card_Background"
            print(f"  {affil_name:<12} ", end="", flush=True)
            ok, info = await bake_socket(page, psd_path, out_dir)
            print(("OK   " if ok else "FAIL ") + info)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
