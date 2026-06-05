"""
Bake per-slot Bases (where present) and per-slot icon glyphs from any
affiliation PSD via headless Photopea.

For each named target layer:
- Hide every layer in the PSD
- Show only the target (and its ancestor groups)
- saveToOE("png") → full-canvas PNG
- Trim to the layer's non-transparent bbox
- Save as PNG; for Base layers also write a sidecar JSON with paste_x/paste_y

This is the icon-glyph + per-slot-Base counterpart to bake_sockets.py. We use
it when we want PSD-accurate renders (with layer effects applied) rather than
the raw raster pixels psd-tools returns. See CLAUDE.md "Headless Photopea for
layer-effect rendering" for the rationale.
"""
import asyncio, base64, io, json, sys
from pathlib import Path
from playwright.async_api import async_playwright
from PIL import Image
import numpy as np

TEMPLATES = Path("templates")
EXTRACTED = Path("extracted")

# (display name, psd filename, output subdir under extracted/)
PSDS = {
    "Federation": ("2e HD Federation v1.psd", "federation"),
    "Non-Aligned": ("2e HD Non-Alligned v1.psd", "nonaligned"),
    "Klingon": ("2e HD Klingon v1.psd", "klingon"),
    "Romulan": ("2e HD Romulan v1.psd", "romulan"),
    "Borg": ("2e HD Borg v1.psd", "borg"),
    "Starfleet": ("2e HD Starfleet v1.psd", "starfleet"),
    "Vidiian": ("2e HD Vidiian v1.psd", "vidiian"),
    "Bajoran":     ("2e_HD_Bajoran_v1.psd",      "bajoran"),
    "Cardassian":  ("2e_HD_Cardassian_v1.psd",   "cardassian"),
    "Dominion":    ("2e_HD_Dominion_v1.psd",     "dominion"),
    "Ferengi":     ("2e HD Ferengi v1.psd",      "ferengi"),
}

# Asset families to try baking. Missing layer paths are skipped silently.
SLOT_ICON_NAMES = {
    "Slot 1": ["Command", "Staff"],
    "Slot 2": ["Maquis", "Earth", "Voyager", "DS9", "TNG", "TOS", "Terok Nor"],
    "Slot 3": ["AU", "Future", "Past"],
    "Slot 4": ["AU", "Earth"],
}

EMBED_HTML = """<!DOCTYPE html><html><body>
<iframe id="pp" src="https://www.photopea.com/?app=1" style="width:100%;height:100vh;border:0"></iframe>
<script>
window._mm = []; window._bins = [];
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


def trim_image(png: bytes):
    im = Image.open(io.BytesIO(png)).convert("RGBA")
    arr = np.array(im); a = arr[:, :, 3]
    ys, xs = np.where(a > 5)
    if len(ys) == 0:
        return None, None
    x0, y0, x1, y1 = xs.min(), ys.min(), xs.max() + 1, ys.max() + 1
    return Image.fromarray(arr[y0:y1, x0:x1]), (int(x0), int(y0))


async def bake_layer(page, layer_path: list[str], out_path: Path,
                     include_sidecar: bool = False) -> tuple[bool, str]:
    path_js = json.dumps(layer_path)
    js = f"""
        var doc = app.activeDocument;
        function hideAll(L){{for(var i=0;i<L.length;i++){{L[i].visible=false;if(L[i].layers)hideAll(L[i].layers);}}}}
        function showPath(L,parts){{
            for(var i=0;i<L.length;i++){{
                if(L[i].name===parts[0]){{
                    L[i].visible=true;
                    if(parts.length===1)return true;
                    return showPath(L[i].layers, parts.slice(1));
                }}
            }}
            return false;
        }}
        hideAll(doc.layers);
        var ok = showPath(doc.layers, {path_js});
        if(!ok) app.echoToOE('NOT_FOUND');
        if(ok) doc.saveToOE("png");
    """
    n_before = await page.evaluate("window._bins.length")
    msgs = await run_script(page, js, max_seconds=30)
    if any("NOT_FOUND" in m for m in msgs):
        return False, "not found"
    n_after = await page.evaluate("window._bins.length")
    if n_after <= n_before:
        return False, "no png"
    b64 = await page.evaluate(f"window.getBinAsB64({n_after - 1})")
    im, paste_xy = trim_image(base64.b64decode(b64))
    if im is None:
        return False, "empty"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    im.save(out_path)
    info = f"{im.size}"
    if include_sidecar and paste_xy is not None:
        sidecar = out_path.with_suffix(".json")
        sidecar.write_text(json.dumps({"paste_x": paste_xy[0], "paste_y": paste_xy[1]}))
        info += f" paste={paste_xy}"
    return True, info


async def bake_for_psd(page, affil_name: str, psd_file: str, outdir_name: str):
    psd_path = TEMPLATES / psd_file
    psd_b64 = base64.b64encode(psd_path.read_bytes()).decode()
    await page.evaluate("window.clearBins()")
    await page.evaluate(f"window.ppSendBin('{psd_b64}')")
    await wait_done(page, 60)
    base_out = EXTRACTED / outdir_name / "assets"

    print(f"=== {affil_name} ===")

    # 1. Per-slot Bases (if present in PSD). Try both layer-tree shapes.
    for slot_n in (1, 2, 3, 4):
        slot = f"Slot {slot_n}"
        # Some PSDs (e.g. Bajoran) name the layer "Base copy N" instead of "Base"
        # for slots 2-4 because the artist duplicated slot 1.
        copy_suffix = "" if slot_n == 1 else (" copy" if slot_n == 2 else f" copy {slot_n - 1}")
        candidates = [
            ["Staffing and Attributes", "Personnel", "Staffing", slot, "Base"],
            ["Staffing and Attributes", "Icons", slot, "Base"],
            ["Staffing and Attributes", "Icons", slot, f"Base{copy_suffix}"],
        ]
        out_path = base_out / "Card_Background" / f"Slot_{slot_n}_Base.png"
        baked = False
        for c in candidates:
            ok, info = await bake_layer(page, c, out_path, include_sidecar=True)
            if ok:
                print(f"  Slot {slot_n} Base   {info}")
                baked = True
                break
        if not baked:
            pass  # silent — many PSDs only have Slot 4 Base

    # 2. Per-slot icon glyphs
    for slot_label, icon_names in SLOT_ICON_NAMES.items():
        slot_n = int(slot_label.split()[-1])
        for icon_name in icon_names:
            for branch in ("Personnel/Staffing", "Ship/Icons"):
                # Skip Personnel/Staffing on Slot 4 / Earth — same as personnel
                # Some PSDs (e.g. Bajoran) flatten the personnel staffing tree:
                # icons sit directly under "Icons/Slot N/..." with no
                # Personnel/Staffing intermediate. Try that as a fallback.
                psd_candidates = [
                    ["Staffing and Attributes"] + branch.split("/") + [slot_label, icon_name],
                ]
                if branch == "Personnel/Staffing":
                    psd_candidates.append(
                        ["Staffing and Attributes", "Icons", slot_label, icon_name]
                    )
                out_path = (base_out / "Staffing_and_Attributes"
                            / branch.replace("/", "_").replace("Personnel_Staffing", "Personnel/Staffing").replace("Ship_Icons", "Ship/Icons")
                            / f"Slot_{slot_n}"
                            / f"{icon_name.replace(' ', '_')}.png")
                for psd_path_parts in psd_candidates:
                    ok, info = await bake_layer(page, psd_path_parts, out_path, include_sidecar=False)
                    if ok:
                        break

    # 3. Ship/Icons per-slot Bases (the affiliation/era socket discs on ships).
    # These are separate from Personnel Slot N Base — different layer subtree.
    for slot_n in (1, 2, 3, 4):
        out_path = (base_out / "Staffing_and_Attributes" / "Ship" / "Icons"
                    / f"Slot_{slot_n}" / "Base.png")
        ok, info = await bake_layer(
            page,
            ["Staffing and Attributes", "Ship", "Icons", f"Slot {slot_n}", "Base"],
            out_path, include_sidecar=True)
        if ok:
            print(f"  Ship Icons Slot {slot_n} Base   {info}")

    # 4. Ship Staffing Command/Staff (ships have up to 5 staffing slots, e.g.
    # Klingon battle cruiser staffing requirements). These layers use layer
    # effects (gold stroke + emboss) and come out zero-alpha from psd-tools.
    for slot_n in range(1, 6):
        for icon_name in ("Command", "Staff"):
            out_path = (base_out / "Staffing_and_Attributes" / "Ship" / "Staffing"
                        / f"Slot_{slot_n}" / f"{icon_name}.png")
            ok, info = await bake_layer(
                page,
                ["Staffing and Attributes", "Ship", "Staffing", f"Slot {slot_n}", icon_name],
                out_path, include_sidecar=False)
            if ok:
                pass  # silent

    # 5. Personnel skill-line "Dot" markers (Skill 1..5). Affiliation-tinted
    # bullets — pure layer-effect circles in the PSD, zero-alpha from psd-tools.
    for slot_n in range(1, 6):
        out_path = (base_out / "Skills_and_Flavor_Text" / "Personnel"
                    / f"Skill_{slot_n}" / "Dot.png")
        ok, info = await bake_layer(
            page,
            ["Skills and Flavor Text", "Personnel", f"Skill {slot_n}", "Dot"],
            out_path, include_sidecar=False)
        if ok:
            print(f"  Skill {slot_n} Dot   {info}")


async def main():
    only = {a.strip() for a in sys.argv[1].split(",")} if len(sys.argv) > 1 else None
    targets = {k: v for k, v in PSDS.items() if only is None or k in only}
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
        for affil_name, (psd_file, outdir_name) in targets.items():
            await bake_for_psd(page, affil_name, psd_file, outdir_name)
            # Close before next PSD
            await run_script(page, "app.activeDocument.close(false);", max_seconds=10)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
