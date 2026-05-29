#!/usr/bin/env python3
"""
Recover bold spans in card game text from the original scans.

DILEMMAS ONLY. Bold game text is a dilemma-specific convention: it marks the
"requirements" a player must meet to overcome the dilemma (skills, attributes,
cost, number of personnel, ...). No other card type uses bold this way, so this
detector — and every heuristic in it — assumes the input is dilemma game text.
Run it only on dilemma rows; feeding it other card types is meaningless.

There is no card-data source that records which words are bold, yet bold is
gameplay-critical. The only ground truth is the font weight printed on the card,
so this measures it from the scan.

WHY PER-CARD SELF-CALIBRATION. These cards span 10+ years and are mostly
*virtual* (never-printed) releases with no common print/scan/render process, so
there is no absolute or cross-card weight scale to calibrate against. Bold vs
regular is only reliably separable *within a single card* (one consistent
render), so every threshold is relative to that card's own regular baseline
(and, finer, its per-line baseline). The same word can be bold in one place and
regular in another, so detection is per word-instance and never keys off word
identity.

RECALL-BIASED. A missed bold is a silent gameplay error; an extra one is obvious
in review and trivially trimmed. So thresholds favour over-flagging, and the
output is a reviewed first pass, not the final word.

Pipeline, per card:
  1. Crop the game-text band of the scan and binarise it (Otsu).
  2. OCR (tesseract TSV) for a box per printed word; char-align to the known
     plain game text so each known token inherits its scan box(es) (robust to
     OCR split/merge around symbols like '>').
  3. Stroke width per token via a distance transform (erosion depth), measured
     against a per-line regular baseline.
  4. Label: requirement-shaped words (skills/species/comparisons/counts) and
     known skills anchor at a low bar; any word anchors if very heavy; bold then
     grows across clearly-heavy neighbours, bridging inline icons and single
     letters. Tokens are merged first ("2 Science", "any attribute>20",
     word>digits). Words just after a requirement lead-in cue ("you have ...",
     "that personnel has ...", "total cost of those personnel ...") anchor/grow
     at a lowered bar (a prior, not a hard rule).
  5. Rules: "to be" is never bold (action separator); "or" is regular except
     "or less/more"; skills in an "(except ...)" list aren't requirements; a
     lone bold word must be requirement-shaped. Grammatical scaffolding (frame
     words: articles, pronouns, "personnel", "has/have", ...) is never bold, so
     only the requirement *nucleus* survives ("a personnel who has 2 Geology" ->
     "...<b>2 Geology</b>"); a span can't begin/end on a dangling "and"/"or".
  6. Re-emit the game text with bold spans wrapped in <b>..</b>.

Hand corrections the pixels can't yield (heavy-print or low-contrast cards) live
in fixture/gametext_bold_overrides.tsv and are merged at write time so they
survive re-running. Output is the sidecar fixture/gametext_bold.tsv that
reconstruct_card.py reads.

Usage:
    python3 detect_bold_gametext.py INPUT.txt [-o OUT.tsv] [--debug CID] [--review-dir DIR]

Requires the `tesseract` CLI on PATH.
"""

import argparse
import re
import subprocess
import sys
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np
from PIL import Image

# Reuse the card loader, photo lookup and design-space constants from the renderer.
import reconstruct_card as rc

# Game-text band in 300-DPI design space (generous: covers game text + the
# italic flavor line beneath it; flavor words simply don't align to known
# tokens and are ignored). Scaled to each scan's own size.
GT_BOX = (115, 660, 640, 805)
UPSCALE = 6
OCR_TMP = Path(".ocr_tmp")
OVERRIDES = Path("fixture/gametext_bold_overrides.tsv")  # hand corrections, merged at write time


def crop_binarized(scan: Image.Image):
    """Crop the game-text band and return (binary PIL image, ink mask ndarray)."""
    W, H = scan.size
    sx, sy = W / rc.BASE_W, H / rc.BASE_H
    box = (int(GT_BOX[0] * sx), int(GT_BOX[1] * sy),
           int(GT_BOX[2] * sx), int(GT_BOX[3] * sy))
    up = scan.convert("L").crop(box).resize(
        ((box[2] - box[0]) * UPSCALE, (box[3] - box[1]) * UPSCALE), Image.LANCZOS)
    a = np.asarray(up, dtype=np.uint8)
    thr = _otsu(a)
    ink = a < thr
    binimg = Image.fromarray(np.where(ink, 0, 255).astype(np.uint8))
    return binimg, ink


def _otsu(a: np.ndarray) -> int:
    hist = np.bincount(a.ravel(), minlength=256).astype(np.float64)
    tot = a.size
    sum_all = np.dot(np.arange(256), hist)
    wB = sumB = 0.0
    best_var, thr = 0.0, 127
    for i in range(256):
        wB += hist[i]
        if wB == 0:
            continue
        wF = tot - wB
        if wF == 0:
            break
        sumB += i * hist[i]
        mB = sumB / wB
        mF = (sum_all - sumB) / wF
        var = wB * wF * (mB - mF) ** 2
        if var > best_var:
            best_var, thr = var, i
    return thr


def stroke_width(ink: np.ndarray) -> float:
    """Mean stroke width of an ink mask via a city-block distance transform.

    Iteratively erode the ink (numpy-only, no scipy/cv2): a pixel's erosion
    depth is its distance to the nearest background pixel, i.e. ~half the local
    stroke width. 2*mean(depth) over the ink ≈ stroke width. Unlike 2*area/
    perimeter this is not biased by letter roundness (the solid interior of
    closed letters like 'e','a','c' no longer inflates the score), which was
    causing all-round words like "each" to read as bold."""
    if ink.sum() == 0:
        return 0.0
    depth = np.zeros(ink.shape, dtype=np.int32)
    cur = ink.copy()
    k = 0
    while cur.any():
        k += 1
        depth[cur] = k
        e = cur.copy()
        e[1:, :] &= cur[:-1, :]
        e[:-1, :] &= cur[1:, :]
        e[:, 1:] &= cur[:, :-1]
        e[:, :-1] &= cur[:, 1:]
        cur = e
    return 2.0 * float(depth[ink].mean())


def ocr_words(binimg: Image.Image):
    """Return [(text, x, y, w, h, line_key)] from tesseract TSV. line_key is
    (block, par, line) so words can be grouped by text line. Writes the temp
    image under the repo (tesseract here can't read /tmp)."""
    OCR_TMP.mkdir(exist_ok=True)
    p = OCR_TMP / "bold_crop.png"
    binimg.save(p)
    r = subprocess.run(["tesseract", str(p), "stdout", "--psm", "6", "tsv"],
                       capture_output=True)
    words = []
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        f = line.split("\t")
        if len(f) == 12 and f[0] != "level" and f[11].strip():
            words.append((f[11], int(f[6]), int(f[7]), int(f[8]), int(f[9]),
                          (f[2], f[3], f[4])))
    return words


_norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())

# Bold/regular is decided purely by measured stroke weight — no word-identity
# rules, because the same word (even a skill) can be bold in one place and not
# another, and bold can span whole clauses including ordinary words. A word is a
# bold candidate when its stroke width exceeds the card's regular baseline by
# RUN_FACTOR. To suppress isolated measurement noise (a lone round word like
# "each" reading a little heavy) we keep a candidate only if it sits in a
# contiguous run of >=2 candidates, OR — for a genuine lone bold skill — it is
# strongly over baseline (SINGLE_FACTOR). A card with no surviving candidate has
# no bold, which is common (e.g. "cost -1" modifier dilemmas with no requirement).
# Anchors (× the word's line baseline). A requirement-shaped word anchors at a
# lower bar; any word (incl. ordinary ones in a whole-clause bold like "that
# personnel has an attribute<5") anchors only if measured very heavy.
ANCHOR_SKILL = 1.19    # a *named* skill/attribute needs only mild evidence to anchor
ANCHOR_SHAPED = 1.25   # other requirement-shaped words (caps, comparisons, digits)
ANCHOR_ANY = 1.40      # any word, if measured very heavy (whole-clause bold)
JOIN = 1.21            # bold grows from an anchor across clearly-heavy neighbours

# Unambiguously-bold override. Every rule below the pixel measurement (frame
# words, lone-word, "(except ...)", "or", edge-trim) is only a *fallback* for
# ambiguous weight — so a token measured this far over its baseline is kept bold
# no matter what those rules decide. The bar must sit above the heavy-print
# artifact band: on the heaviest cards ordinary non-bold words read up to ~1.60
# (e.g. 1R007 "personnel" 1.60), while a genuine strong bold like 50V001's
# dynamic requirement "skill" reads 1.85 — so 1.70 clears the noise with margin.
# (Narrow window, calibrated on the current deck; revisit if cards are added.)
OVERRIDE = 1.70

# Requirement lead-in cue (heuristic #1). Certain phrases grammatically
# introduce a requirement, so a borderline word just after them is more likely
# bold. A conjugation of "have" ("you have", "unless you have", "that personnel
# has", "he or she has", "they have") or "total cost of those personnel" opens a
# requirement clause; inside that window the skill/shape anchor bars and the
# JOIN growth bar are scaled down by CUE_DISCOUNT. This is a prior on the pixel
# measurement, never a hard rule — a word that reads clearly regular still won't
# anchor, and ANCHOR_ANY (whole-clause heavy bold) is left undiscounted.
HAVE_CUE = {"have", "has"}
CUE_DISCOUNT = 0.92

# ---------------------------------------------------------------------------
# POTENTIAL HEURISTICS — NOT IMPLEMENTED YET
# Ideas for disambiguating borderline cases (where the measured weight is too
# close to call), to revisit if the current rules leave too much residue. These
# would act as priors (nudge the threshold) layered on the pixel measurement,
# never as hard rules.
#
# 1. (To investigate — less certain.) The END of a bold requirement is usually an
#    "or" or a comma. Could help decide where a bold span should stop (and pair
#    with the existing "or"/comma handling), but needs verification first.
# ---------------------------------------------------------------------------


def align_boxes_to_tokens(known_tokens, ocr_words):
    """Map each known token index -> list of (x, y, w, h, line_key) covering it,
    by aligning the normalised known text to the normalised OCR text at the
    character level (robust to OCR splitting 'Cunning>11' into 'Cunning>1' + '1',
    etc.)."""
    kchars, kowner = [], []
    for ti, tok in enumerate(known_tokens):
        for ch in _norm(tok):
            kchars.append(ch)
            kowner.append(ti)
    kstr = "".join(kchars)

    ochars, obox = [], []
    for (txt, x, y, w, h, line) in ocr_words:
        for ch in _norm(txt):
            ochars.append(ch)
            obox.append((x, y, w, h, line))
    ostr = "".join(ochars)

    token_boxes = {ti: [] for ti in range(len(known_tokens))}
    sm = SequenceMatcher(None, kstr, ostr, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag != "equal":
            continue
        for k in range(i1, i2):
            token_boxes[kowner[k]].append(obox[j1 + (k - i1)])
    return token_boxes


def token_thickness(ink: np.ndarray, boxes) -> float:
    """Mean stroke width over a token's box(es), computed on the union of the
    boxes' ink so overlapping glyphs aren't double-counted."""
    if not boxes:
        return 0.0
    union = np.zeros_like(ink)
    for (x, y, w, h, _line) in set(boxes):
        union[y:y + h, x:x + w] |= ink[y:y + h, x:x + w]
    return stroke_width(union)


def token_line(boxes):
    """The OCR line a token mostly sits on (mode of its boxes' line keys)."""
    if not boxes:
        return None
    from collections import Counter
    return Counter(b[4] for b in boxes).most_common(1)[0][0]


# 2E skills and attributes. Used only as a precision gate on *lone* bold words
# (heuristic: a single bold word that isn't a skill/attribute is not a
# requirement). It never forces anything bold — the pixels still decide — so it
# doesn't conflict with "the same word can be bold in one place and not another".
SKILLS = {
    "acquisition", "anthropology", "archaeology", "astrometrics", "biology",
    "diplomacy", "engineer", "exobiology", "geology", "honor", "intelligence",
    "law", "leadership", "medical", "navigation", "officer", "physics",
    "programming", "science", "security", "telepathy", "transporters",
    "treachery", "youth",
    "integrity", "cunning", "strength", "range", "weapons", "shields",  # attributes
}


# "to be" introduces the action applied to the cards a requirement identified
# ("... to be stopped/killed/discarded"). It is never part of the requirement,
# so it's forced non-bold and acts as a barrier between a requirement and its
# action.
ACTION_BARRIER = {"to", "be"}


# Grammatical scaffolding around a requirement: articles, demonstratives,
# relativisers, pronouns, copula/auxiliary "have" forms, and the structural noun
# "personnel". These are never the requirement *nucleus* (the skill/attribute/
# comparison/count). Gameplay only cares that the nucleus is bold, so we never
# bold the frame — even on cards that printed a whole clause bold: "a personnel
# who has 2 Geology" -> "a personnel who has <b>2 Geology</b>". Frame words can't
# anchor a span and are stripped from any span at the end (which also kills
# heavy-print over-boxes like 1R007's "personnel who"). "and"/"or"/commas are
# deliberately excluded — they are list connectors *between* nuclei and stay
# bold inside a multi-element requirement ("Geology and Cunning>11").
FRAME_WORDS = {
    "a", "an", "the",
    "this", "that", "these", "those",
    "who", "whom", "whose", "which",
    "he", "she", "it", "its", "they", "them", "you", "your", "his", "her", "their",
    "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did",
    "of", "with", "each", "personnel",
}


def is_requirement_word(tok):
    """A lone bold token is a real requirement only if it's a skill/attribute, a
    comparison (Cunning>11, attribute<5) or a count+skill (2 Science)."""
    if '>' in tok or '<' in tok:
        return True
    if re.match(r'\d+\s+[A-Z][a-z]', tok):
        return True
    return re.sub(r'[^A-Za-z]', '', tok).lower() in SKILLS


def icon_only(tok):
    """True if a token is nothing but inline icon(s), e.g. '[HQ]' or '[AQ][GQ]'.
    Such tokens carry no scan glyphs, so they can't be measured and are treated
    as transparent when growing a bold span."""
    return bool(re.fullmatch(r'(\[[^\]]+\])+', tok))


def merge_icon_runs(tokens):
    """Collapse consecutive icon tokens with no word between them into one token
    (e.g. ['[AQ]', '[GQ]'] -> ['[AQ][GQ]']), so a run of icons bridges as a
    single unit."""
    out, i = [], 0
    while i < len(tokens):
        if icon_only(tokens[i]):
            j = i
            while j < len(tokens) and icon_only(tokens[j]):
                j += 1
            out.append("".join(tokens[i:j]))
            i = j
        else:
            out.append(tokens[i])
            i += 1
    return out


def merge_any_attribute(tokens):
    """Treat "any attribute" as one token — it's an attribute reference like
    Cunning/Strength/Integrity, and "any attribute>20" is a single attribute
    expression (the comparison and digits already bind via the < > spacing
    normalisation)."""
    out, i = [], 0
    while i < len(tokens):
        if (i + 1 < len(tokens) and re.fullmatch(r'(?i:any)', tokens[i])
                and re.match(r'(?i:attribute)', tokens[i + 1])):
            out.append(tokens[i] + " " + tokens[i + 1])
            i += 2
        else:
            out.append(tokens[i])
            i += 1
    return out


def merge_skill_counts(tokens):
    """Merge a bare digit immediately followed by a Capitalised skill word into a
    single token, e.g. ["2", "Science"] -> ["2 Science"]. In 2E "2 Science" is
    one requirement (two Science), so it should be measured and bolded as a unit
    rather than split (which let the count anchor while the skill was dropped).
    A digit followed by a lowercase word ("2 or less", "add 10 to") is left
    alone — that's a quantity, not a skill count."""
    out = []
    i = 0
    while i < len(tokens):
        if (i + 1 < len(tokens) and re.fullmatch(r'\d+', tokens[i])
                and re.match(r'[A-Z][a-z]', tokens[i + 1])):
            out.append(tokens[i] + " " + tokens[i + 1])
            i += 2
        else:
            out.append(tokens[i])
            i += 1
    return out


def req_shaped(tok, sentence_initial):
    """Whether a token *looks like* a requirement element — used only as a prior
    that lowers the bold bar, never as a hard rule (the pixels still decide).
    Requirement bold marks skills (Capitalised words, but not the capitalised
    word that merely starts a sentence), attributes/comparisons (Cunning>11,
    attribute<5) and digit counts incl. merged "2 Science". A spelled-out number
    like 'three' is a quantity, not a requirement, so it is NOT shaped."""
    if '>' in tok or '<' in tok:
        return True
    if re.match(r'\d+\s+[A-Z][a-z]', tok):   # merged count+skill, e.g. "2 Science"
        return True
    if re.sub(r'[^A-Za-z0-9]', '', tok).isdigit():   # a number (e.g. cost "5")
        return True
    if re.match(r'[A-Z][a-z]', tok) and not sentence_initial:
        return True
    return False


def requirement_cue_window(tokens, sentence_initial):
    """Bool per token: True when the token sits in a requirement clause opened
    by a lead-in cue (heuristic #1) — a conjugation of "have" ("you have",
    "that personnel has", "unless you have", ...) or "total cost of those
    personnel". The window starts just after the cue and runs to the next
    clause boundary (a sentence end or the "to be" action barrier), since the
    requirement ends where the action it gates begins. Used only to scale the
    anchor/join bars down for the cued words."""
    norm = [_norm(t) for t in tokens]
    starts = []
    for ti in range(len(tokens)):
        if norm[ti] in HAVE_CUE:
            starts.append(ti + 1)
    phrase = ["total", "cost", "of", "those", "personnel"]
    for ti in range(len(tokens) - len(phrase) + 1):
        if norm[ti:ti + len(phrase)] == phrase:
            starts.append(ti + len(phrase))

    cued = [False] * len(tokens)
    for s in starts:
        ti = s
        while ti < len(tokens):
            if ti != s and sentence_initial[ti]:
                break
            if norm[ti] in ACTION_BARRIER:
                break
            cued[ti] = True
            ti += 1
    return cued


def wrap_bold(tokens, is_bold) -> str:
    """Rejoin tokens, wrapping each maximal run of bold tokens in <b>..</b>."""
    out, i = [], 0
    while i < len(tokens):
        if is_bold[i]:
            j = i
            while j < len(tokens) and is_bold[j]:
                j += 1
            out.append("<b>" + " ".join(tokens[i:j]) + "</b>")
            i = j
        else:
            out.append(tokens[i])
            i += 1
    return " ".join(out)


def _write_review(binimg, tokens, token_boxes, is_bold, path):
    """Save the binarised crop with each detected-bold token boxed in red, so a
    human can check the flags against the actual printed weight."""
    from PIL import ImageDraw
    rgb = binimg.convert("RGB")
    d = ImageDraw.Draw(rgb)
    for ti in range(len(tokens)):
        if not is_bold[ti]:
            continue
        for (x, y, w, h, _line) in token_boxes[ti]:
            d.rectangle([x, y, x + w, y + h], outline=(220, 0, 0), width=3)
    rgb.save(path)


def detect(row, debug=False, review_dir=None):
    """Return marked-up game text for one card, or None if no scan/text."""
    # Curly braces wrap named-card references ("{Nebula}", "{Bajor}"); they are
    # render-only markup that reconstruct_card.py strips before drawing. Drop
    # them here too so a braced name is measured/shaped like a plain word
    # (otherwise the leading "{" stops it anchoring as a requirement).
    gametext = rc.strip_braces(row["gametext"].strip())
    if not gametext:
        return None
    try:
        scan = Image.open(rc.find_photo(row))
    except SystemExit:
        return None

    binimg, ink = crop_binarized(scan)
    words = ocr_words(binimg)
    if not words:
        return None

    # #1: bind a comparison and its digits to the word before it as one token
    # (strip any spaces around < and >, so "Cunning > 11" -> "Cunning>11").
    gt = re.sub(r'\s*([<>])\s*', r'\1', gametext)
    tokens = merge_skill_counts(merge_any_attribute(merge_icon_runs(gt.split())))
    # Tokens that carry no scan glyphs (inline [ICON]s) can't be measured.
    measurable = [bool(_norm(t)) and not icon_only(t) for t in tokens]
    token_boxes = align_boxes_to_tokens(tokens, words)

    thicks = {ti: token_thickness(ink, token_boxes[ti])
              for ti in range(len(tokens)) if measurable[ti] and token_boxes[ti]}
    if len(thicks) < 2:
        return None

    # --- E. Per-line baseline ---------------------------------------------
    # Scans/renders vary line to line; calibrate "regular" per text line so a
    # uniformly darker line doesn't read as bold. A line's baseline is the
    # median of its words that fall in the card's lower half (i.e. probably
    # regular); lines without enough such words fall back to the card baseline.
    card_vals = np.array(sorted(thicks.values()))
    card_med = float(np.median(card_vals))
    card_base = float(np.median(card_vals[card_vals <= card_med]))
    lines = {ti: token_line(token_boxes[ti]) for ti in thicks}
    line_regs = {}
    for ti, lk in lines.items():
        if thicks[ti] <= card_med:
            line_regs.setdefault(lk, []).append(thicks[ti])
    line_base = {lk: float(np.median(v)) for lk, v in line_regs.items() if len(v) >= 2}
    ratio = {ti: thicks[ti] / line_base.get(lines[ti], card_base) for ti in thicks}

    # --- C. Requirement-shape prior ---------------------------------------
    sentence_initial = [True] * len(tokens)
    for ti in range(1, len(tokens)):
        sentence_initial[ti] = tokens[ti - 1].rstrip('"\'').endswith(('.', '!', '?', ':'))
    shaped = {ti: req_shaped(tokens[ti], sentence_initial[ti]) for ti in thicks}
    cued = requirement_cue_window(tokens, sentence_initial)

    # --- B. Anchors + propagation -----------------------------------------
    # Anchor = a requirement-shaped word measured clearly heavy. Bold then grows
    # from anchors across adjacent heavy words (JOIN). A frame/scaffolding word
    # can never anchor (it isn't a requirement nucleus), so heavy-print noise on
    # ordinary words like "personnel who" can't seed a span; frame words that get
    # swept in by JOIN are stripped at the end (see FRAME_WORDS).
    is_skill = {ti: re.sub(r'[^A-Za-z]', '', tokens[ti]).lower() in SKILLS for ti in thicks}
    is_frame = {ti: _norm(tokens[ti]) in FRAME_WORDS for ti in thicks}
    is_bold = [False] * len(tokens)
    for ti in thicks:
        d = CUE_DISCOUNT if cued[ti] else 1.0  # heuristic #1: lower bar after a requirement cue
        if (ratio[ti] > ANCHOR_ANY and not is_frame[ti]) \
                or (is_skill[ti] and ratio[ti] > ANCHOR_SKILL * d) \
                or (shaped[ti] and ratio[ti] > ANCHOR_SHAPED * d):
            is_bold[ti] = True

    def bridgeable(tok):
        # Icons carry no glyphs; single-letter words ("a") are too small to
        # measure reliably. Neither should break a bold span, so we bridge over
        # them (and mark them bold) when the word beyond qualifies.
        return icon_only(tok) or len(_norm(tok)) <= 1

    def next_word(pos, step):
        """Nearest substantial token from pos in direction step, plus the
        bridgeable tokens (icons / single letters) skipped to reach it."""
        skipped, j = [], pos + step
        while 0 <= j < len(tokens) and bridgeable(tokens[j]):
            skipped.append(j)
            j += step
        return (j if 0 <= j < len(tokens) else None), skipped

    def requirement_continues(p):
        """Does the requirement list continue at/after token p? True when the
        nearest substantive token there — skipping and/or connectors and
        bridgeable icons/single letters — is requirement-shaped. This tells an
        *internal* list comma ("Astrometrics, Engineer, and Cunning>42", whose
        comma is followed by more requirement elements) from the *terminal*
        comma that ends the requirement (followed by the action clause, e.g.
        "..., randomly select ...")."""
        j = p
        while 0 <= j < len(tokens) and (bridgeable(tokens[j])
                                        or _norm(tokens[j]) in ("and", "or")):
            j += 1
        return 0 <= j < len(tokens) and req_shaped(tokens[j], sentence_initial[j])

    changed = True
    while changed:
        changed = False
        for ti in range(len(tokens)):
            if not is_bold[ti]:
                continue
            for step in (-1, 1):
                nb, icons = next_word(ti, step)
                if nb is None or is_bold[nb]:
                    continue
                # heuristic #1: lower the join bar inside a cue window. Across a
                # comma the discount applies only if the requirement *continues*
                # past it (the next substantive token is requirement-shaped) — so
                # a list connector ("Engineer, and Cunning>42") still joins, but
                # the terminal comma's action verb ("...2 Physics, randomly
                # select...") must clear the full bar. Anchors stay discounted
                # regardless, so a real skill after an internal comma benefits.
                after_comma = nb > 0 and tokens[nb - 1].rstrip('"\'').endswith(',')
                discount = cued[nb] and (not after_comma or requirement_continues(nb))
                join_bar = JOIN * (CUE_DISCOUNT if discount else 1.0)
                if nb in ratio and ratio[nb] > join_bar \
                        and _norm(tokens[nb]) not in ACTION_BARRIER \
                        and not sentence_initial[max(ti, nb)]:  # don't cross "." / "to be"
                    is_bold[nb] = True
                    for ic in icons:        # bridge the skipped icon(s)
                        is_bold[ic] = True
                    changed = True

    # Bold any bridgeable token (icon / single letter) sitting between two bold
    # words, to keep the span contiguous.
    for ti in range(len(tokens)):
        if bridgeable(tokens[ti]) and not is_bold[ti]:
            left, _ = next_word(ti, -1)
            right, _ = next_word(ti, 1)
            if left is not None and right is not None and is_bold[left] and is_bold[right]:
                is_bold[ti] = True

    # "to be" (the action separator) is never bold.
    for ti in range(len(tokens)):
        if _norm(tokens[ti]) in ACTION_BARRIER:
            is_bold[ti] = False

    # Strip grammatical scaffolding from every span: only the requirement nucleus
    # (and the connectors between nuclei) stays bold, so "a personnel who has 2
    # Geology" keeps just "2 Geology" — the gameplay-relevant part — whether the
    # card printed the whole clause bold or only the skill. See FRAME_WORDS.
    for ti in range(len(tokens)):
        if is_frame.get(ti, _norm(tokens[ti]) in FRAME_WORDS):
            is_bold[ti] = False

    # "or" inside a requirement is almost always non-bold (it separates
    # alternative requirements). The only exception is the quantity phrases
    # "or less" / "or more" (e.g. "5 or more", "one or more Geology").
    for ti in range(len(tokens)):
        if is_bold[ti] and _norm(tokens[ti]) == "or":
            nxt = _norm(tokens[ti + 1]) if ti + 1 < len(tokens) else ""
            if nxt not in ("less", "more"):
                is_bold[ti] = False

    # Skills listed in an "(except ...)" clause are exclusions, not requirements,
    # so nothing inside that parenthetical is bold.
    in_except = False
    for ti in range(len(tokens)):
        if tokens[ti].lower().startswith("(except"):
            in_except = True
        if in_except:
            is_bold[ti] = False
        if ")" in tokens[ti]:
            in_except = False

    # #2: a lone bold word that isn't requirement-shaped is noise. Requirement-
    # shaped covers skills *and* species (any capitalised mid-sentence word),
    # comparisons and counts — so "Officer"/"Android"/"Integrity<4" survive while
    # lowercase/sentence-initial noise ("pile", "each", "Randomly") is dropped.
    ti = 0
    while ti < len(tokens):
        if not is_bold[ti]:
            ti += 1
            continue
        j = ti
        while j < len(tokens) and is_bold[j]:
            j += 1
        words = [k for k in range(ti, j) if not icon_only(tokens[k])]
        if len(words) == 1 and not req_shaped(tokens[words[0]], sentence_initial[words[0]]):
            for k in range(ti, j):
                is_bold[k] = False
        ti = j

    # A span must not begin or end on a bare "and"/"or": stripping scaffolding
    # can leave a connector dangling ("Weapons>7 and you command" -> "Weapons>7
    # and"). A connector is only meaningful *between* two bold nuclei, so trim it
    # off the edges (repeatedly, in case several stack up).
    def _conn(ti):
        return _norm(tokens[ti].rstrip(',')) in ("and", "or")
    trimmed = True
    while trimmed:
        trimmed = False
        ti = 0
        while ti < len(tokens):
            if not is_bold[ti]:
                ti += 1
                continue
            j = ti
            while j < len(tokens) and is_bold[j]:
                j += 1
            if _conn(ti):
                is_bold[ti] = False           # leading connector
                trimmed = True
            elif _conn(j - 1):
                is_bold[j - 1] = False        # trailing connector
                trimmed = True
            ti = j

    # Unambiguously-bold override (last word): a token measured far over its
    # baseline is trusted over every suppression rule above — they are only
    # fallbacks for ambiguous weight. This recovers genuine bold the shape/frame
    # heuristics can't see, e.g. 50V001's dynamic requirement "that skill".
    for ti in thicks:
        if ratio[ti] >= OVERRIDE:
            is_bold[ti] = True

    if debug:
        print(f"  card_base={card_base:.2f} anchor_shaped>{ANCHOR_SHAPED} "
              f"anchor_any>{ANCHOR_ANY} join>{JOIN}")
        for ti, tok in enumerate(tokens):
            r = ratio.get(ti)
            tags = []
            if ti in shaped and shaped[ti]:
                tags.append("shape")
            if cued[ti]:
                tags.append("cue")
            if _norm(tok) in FRAME_WORDS:
                tags.append("frame")
            if is_bold[ti]:
                tags.append("BOLD")
            print(f"    {tok:16} {('%.2f' % r) if r is not None else '  - ':>5} "
                  f"{' '.join(tags)}")

    if review_dir is not None:
        _write_review(binimg, tokens, token_boxes, is_bold,
                      Path(review_dir) / f"{row['CollectorsInfo']}.png")

    return wrap_bold(tokens, is_bold)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", type=Path, help="card data file (tab-separated)")
    ap.add_argument("-o", "--out", type=Path, default=rc.GAMETEXT_MARKUP,
                    help=f"output sidecar TSV (default {rc.GAMETEXT_MARKUP})")
    ap.add_argument("--debug", metavar="CID",
                    help="print per-word thickness for one CollectorsInfo")
    ap.add_argument("--review-dir", type=Path,
                    help="also write per-card crops with detected-bold words boxed")
    args = ap.parse_args()
    if args.review_dir:
        args.review_dir.mkdir(parents=True, exist_ok=True)

    if not args.input.exists():
        raise SystemExit(f"Input file not found: {args.input}")

    # Hand-reviewed corrections that the detector can't get from the pixels
    # (low-contrast/heavy-print cards, semantic calls). These always win and
    # survive re-running the detector.
    overrides = rc.load_gametext_markup(OVERRIDES)

    rows = rc.load_rows(args.input)
    results = []
    for row in rows:
        cid = row["CollectorsInfo"]
        dbg = args.debug == cid
        if dbg:
            print(f"[{cid}] {row['Name']}")
        marked = detect(row, debug=dbg, review_dir=args.review_dir)
        if cid in overrides:
            marked = overrides[cid]
            print(f"  {cid}: (override) {marked.count('<b>')} bold span(s)")
        elif marked is None:
            print(f"  ! {cid}: no detection (missing scan/OCR)")
            continue
        else:
            print(f"  {cid}: {marked.count('<b>')} bold span(s)")
        results.append((cid, marked))

    if args.debug:
        return

    with args.out.open("w") as f:
        f.write("CollectorsInfo\tgametext\n")
        f.write("# Auto-detected bold spans from scans (detect_bold_gametext.py),\n")
        f.write(f"# with hand corrections from {OVERRIDES.name} merged in.\n")
        for cid, marked in results:
            f.write(f"{cid}\t{marked}\n")
    print(f"\nWrote {len(results)} row(s) to {args.out} "
          f"({len(overrides)} override(s) applied).")


if __name__ == "__main__":
    main()
