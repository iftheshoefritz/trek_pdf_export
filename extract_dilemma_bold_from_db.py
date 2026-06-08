"""Extract dilemma gametext (with <b> markup) from the achievements DB dump.

The 2E `card` table in `db_for_achievements.sql` stores dilemma gametext with
the original <b>...</b> bold runs intact — the source-of-truth for bold
markup, which `cards_with_processed_columns.txt` does not carry.

This script joins DB Dilemma rows to the master file by title (handling
embedded quotes, reprint suffixes, etc.) and writes
`fixture/gametext_bold.tsv` (CollectorsInfo<TAB>gametext), which
`reconstruct_card.py` reads via its `GAMETEXT_MARKUP` sidecar.
"""
import html
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SQL_PATH = ROOT / "db_for_achievements.sql"
MASTER_PATH = ROOT / "cards_with_processed_columns.txt"
OUT_PATH = ROOT / "fixture" / "gametext_bold.tsv"

# Each row in `card` is one tuple inside a multi-row INSERT. Tuples span a
# single line in this dump (phpMyAdmin default). Split out the dilemma rows
# with a tuple-level regex; field-level parsing then handles SQL escapes.
TUPLE_RE = re.compile(r"\((\d+,\s*(?:NULL|\d+)\s*,.*?)\)(?=,\s*\(|;)", re.S)

# Fields, in order, as defined in the CREATE TABLE for `card`.
FIELDS = [
    "cardID", "frontsideID", "editionID", "edition_collectorsinfo",
    "rarity", "number", "ptcode", "cardtype", "cost", "title", "subtitle",
    "keywords", "gametext", "lore", "uniquedot", "backwards", "legal",
    "legalNote", "searchable", "foil", "altimg", "sideways", "printable",
    "printdate", "cotd", "spoilDate", "errataDate", "errataID", "imagePath",
    "errataPath", "pdf3UP", "pdf9UP", "altCardID", "flagAsNew", "displayLink",
    "notation", "tourney_legal", "lore_series", "lore_episode",
    "source_series", "source_episode", "proofnum", "proofby", "prooflock",
]

CI_SET_RE = re.compile(r"^(\d+)[A-Z]\d+")


def norm_title(s: str) -> str:
    """Normalise a card title for cross-source matching: drop embedded quote
    chars (DB stores some titles with literal `"..."` wrapping; the master
    file's TSV-escaped form differs from the SQL-escaped form for titles
    containing apostrophes/quotes), collapse whitespace, lowercase."""
    s = s.replace('"', "").replace("''", "'")
    return re.sub(r"\s+", " ", s).strip().lower()


def load_master_titles() -> dict:
    """Map normalised title -> CollectorsInfo for sets 1-14 Dilemmas in the
    master data file. Titles in the DB sometimes have trailing whitespace or
    differ in case; normalise both sides to lowercase + collapsed whitespace.
    """
    out = {}
    with MASTER_PATH.open(encoding="utf-8") as f:
        header = f.readline().rstrip("\n").split("\t")
        idx = {h: i for i, h in enumerate(header)}
        for line in f:
            parts = [c.strip('"') for c in line.rstrip("\n").split("\t")]
            if parts[idx["Type"]] != "Dilemma":
                continue
            ci = parts[idx["CollectorsInfo"]]
            m = CI_SET_RE.match(ci)
            if not m or not (1 <= int(m.group(1)) <= 14):
                continue
            out[norm_title(parts[idx["Name"]])] = ci
    return out


def split_sql_values(body: str):
    """Yield SQL field values from one tuple body (between the outer parens)."""
    out, i, n = [], 0, len(body)
    while i < n:
        # skip leading whitespace + comma
        while i < n and body[i] in " \t,":
            i += 1
        if i >= n:
            break
        if body[i] == "'":
            # quoted string with backslash + doubled-quote escapes
            i += 1
            buf = []
            while i < n:
                c = body[i]
                if c == "\\" and i + 1 < n:
                    buf.append(body[i + 1])
                    i += 2
                elif c == "'":
                    if i + 1 < n and body[i + 1] == "'":
                        buf.append("'")
                        i += 2
                    else:
                        i += 1
                        break
                else:
                    buf.append(c)
                    i += 1
            out.append("".join(buf))
        else:
            j = i
            while j < n and body[j] != ",":
                j += 1
            tok = body[i:j].strip()
            out.append(None if tok == "NULL" else tok)
            i = j
    return out


def clean_gametext(raw: str) -> str:
    """Decode HTML entities, drop &nbsp; padding, normalise whitespace.

    Preserves <b> / </b> tags verbatim; strips other HTML.
    """
    t = raw.replace("&nbsp;", " ")
    # protect bold tags before stripping other markup
    t = re.sub(r"<\s*b\s*>", "\x00B\x00", t, flags=re.I)
    t = re.sub(r"<\s*/\s*b\s*>", "\x00b\x00", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    t = t.replace("\x00B\x00", "<b>").replace("\x00b\x00", "</b>")
    t = re.sub(r"[ \t]+", " ", t).strip()
    return t


def main():
    # Match DB rows back to master-file Dilemmas in sets 1-14 by title — the
    # DB has many imagePath shapes (VP reprints, errata, WYLB, lowercase
    # variants...), and the bold markup is the same across printings of a
    # given title. Using the master's CollectorsInfo as the join key collapses
    # all printings into one row per card.
    master = load_master_titles()
    matched_titles = set()

    # When multiple DB rows match the same title, prefer the one whose markup
    # is most recent. Score by (has_bold, errataDate, printdate, cardID).
    candidates = {}  # ci -> (score_tuple, cleaned_gametext)

    in_card = False
    tuple_line = re.compile(r"^\((.*)\)[,;]\s*$")
    with SQL_PATH.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("INSERT INTO `"):
                in_card = line.startswith("INSERT INTO `card` (")
                continue
            if not in_card:
                continue
            m = tuple_line.match(line)
            if not m:
                continue
            vals = split_sql_values(m.group(1))
            if len(vals) != len(FIELDS):
                continue
            rec = dict(zip(FIELDS, vals))
            if rec["cardtype"] != "Dilemma":
                continue
            title = norm_title(rec["title"] or "")
            if title not in master:
                continue
            matched_titles.add(title)
            gametext = rec["gametext"] or ""
            if "<b>" not in gametext.lower():
                continue
            ci = master[title]
            score = (
                rec.get("errataDate") or "0000-00-00",
                rec.get("printdate") or "0000-00-00",
                int(rec.get("cardID") or 0),
            )
            cur = candidates.get(ci)
            if cur is None or score > cur[0]:
                candidates[ci] = (score, clean_gametext(gametext))

    seen = {ci: gt for ci, (_, gt) in candidates.items()}

    # Diagnostics: master Dilemmas with no DB match at all, and those that
    # matched in the DB but never had <b> markup in any printing.
    unmatched = [ci for t, ci in master.items() if t not in matched_titles]
    no_bold = [master[t] for t in matched_titles if master[t] not in candidates]

    def sort_key(ci):
        m = re.match(r"^(\d+)([A-Z])(\d+)(.*)$", ci)
        if not m:
            return (999, "Z", 9999, ci)
        return (int(m.group(1)), m.group(2), int(m.group(3)), m.group(4))

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w", encoding="utf-8") as f:
        f.write("CollectorsInfo\tgametext\n")
        f.write("# Dilemma bold markup extracted from db_for_achievements.sql.\n")
        for ci in sorted(seen, key=sort_key):
            f.write(f"{ci}\t{seen[ci]}\n")

    print(f"wrote {len(seen)} rows to {OUT_PATH.relative_to(ROOT)}")
    print(f"master sets 1-14 dilemmas: {len(master)}")
    print(f"  matched to DB title:     {len(matched_titles)}")
    print(f"    of which had <b>:      {len(seen)}")
    print(f"    matched but no bold:   {len(no_bold)}")
    print(f"  no DB title match:       {len(unmatched)}")
    if unmatched:
        print("  unmatched CollectorsInfo (first 10):", sorted(unmatched)[:10])


if __name__ == "__main__":
    main()
