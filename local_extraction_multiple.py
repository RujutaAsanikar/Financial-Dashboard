#!/usr/bin/env python3
"""
Extract structured transaction data from a bank statement with no LLM and no
network access.

Two paths, chosen automatically per page:
  * PDF with a text layer -> pdfplumber reads words and their exact coordinates
  * Image, or scanned PDF -> Tesseract OCR produces words and coordinates

From there the logic is identical and purely geometric: group words into rows,
find where the amount columns sit by clustering the right edges of currency
tokens, then assign every token to a column. Bank statements are machine-
generated and rigidly aligned, which is what makes this reliable.

Usage:
    python local_extraction.py statement.pdf
    python local_extraction.py statement.png --debug
    python local_extraction.py                    # auto-detect a single file

Writes transaction_data.json to the working folder.

Requires: pip install pdfplumber pytesseract pillow

Tesseract itself is expected as a portable copy in the project folder:

    C:\\Financial-Dashboard\\
        local_extraction.py
        tesseract\\
            tesseract.exe
            tessdata\\
                eng.traineddata

The path is resolved relative to this script, so the project can be moved or
cloned anywhere without editing it. Override with --tesseract-dir or the
TESSERACT_DIR environment variable. A system-wide install on PATH also works.
Only images and scanned PDFs need Tesseract; text-layer PDFs need none of it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
from pathlib import Path

# ============================================
# CONFIGURATION
# ============================================

DEFAULT_OUTPUT = "transaction_data.json"

SUPPORTED_TYPES = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}

# ============================================
# TESSERACT LOCATION
# ============================================

# Where the Tesseract build lives. This is a portable copy kept inside the
# project folder rather than a system-wide install, so it is resolved relative
# to this script: the project can be cloned or moved without editing paths.
# The folder is expected to contain tesseract.exe and a tessdata subfolder.
TESSERACT_DIR = "tesseract"

# Checked in order; the first directory that exists wins.
#   1. --tesseract-dir on the command line
#   2. the TESSERACT_DIR environment variable
#   3. TESSERACT_DIR above, relative to this script, then to the working folder
#   4. the absolute fallbacks below
# If none exist, Tesseract is assumed to be on PATH.
TESSERACT_FALLBACK_DIRS = [
    r"C:\Financial-Dashboard\tesseract",
    r"C:\Program Files\Tesseract-OCR",
]

SCRIPT_DIR = Path(__file__).resolve().parent

# Set from the command line in main(); read by the OCR path.
TESSERACT_DIR_OVERRIDE: str | None = None
_TESSERACT_CONFIGURED = False

# OCR settings. Tesseract's LSTM engine peaks at roughly 300 DPI equivalent,
# which for a typical statement page means about 2100px wide. Low-resolution
# images are scaled up to meet that; already-large rasters are left alone,
# since interpolating an image twice costs accuracy rather than gaining it.
TARGET_OCR_WIDTH = 2100
MAX_OCR_UPSCALE = 4.0
PDF_RASTER_DPI = 300

# Column geometry tolerances, as a fraction of page width.
COLUMN_TOLERANCE = 0.02
ROW_TOLERANCE = 0.6  # fraction of median text height

# Header keywords used to label the detected columns.
HEADER_KEYWORDS = {
    "date": ("date",),
    "description": ("description", "transaction", "particulars", "details", "activity", "narrative"),
    "reference": ("ref", "reference", "cheque", "check", "no.", "number"),
    "withdrawal": ("withdrawal", "withdrawals", "debit", "debits", "paid out", "charges", "payments"),
    "deposit": ("deposit", "deposits", "credit", "credits", "paid in"),
    "balance": ("balance", "bal"),
}

# Rows that are summary lines rather than transactions.
OPENING_MARKERS = ("previous balance", "opening balance", "balance forward", "brought forward", "beginning balance")
CLOSING_MARKERS = ("closing balance", "ending balance", "new balance", "final balance", "balance carried")
TOTAL_MARKERS = ("total", "totals")

CURRENCY_RE = re.compile(r"^[\(\-\$€£]*\d{1,3}(?:,\d{3})*(?:\.\d{2})?[\)\-]?$|^[\(\-\$€£]*\d+\.\d{2}[\)\-]?$")
INTEGER_RE = re.compile(r"^\d{1,8}$")

# Glyphs that are table rules or scan artifacts rather than content. A token
# made up entirely of these is discarded during OCR cleanup.
BORDER_GLYPHS = set("|_—―–=~<>*·•\u00a6\u2014\u2015 ")

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


# ============================================
# TESSERACT SETUP
# ============================================

def candidate_tesseract_dirs() -> list[Path]:
    """Every place worth looking for the Tesseract folder, in priority order."""
    candidates: list[Path] = []

    def add(value) -> None:
        if not value:
            return
        path = Path(value).expanduser()
        if path.is_absolute():
            candidates.append(path)
        else:
            # A relative setting is resolved against the script first so the
            # project folder can be moved or cloned anywhere.
            candidates.append(SCRIPT_DIR / path)
            candidates.append(Path.cwd() / path)

    add(TESSERACT_DIR_OVERRIDE)
    add(os.environ.get("TESSERACT_DIR"))
    add(TESSERACT_DIR)
    for fallback in TESSERACT_FALLBACK_DIRS:
        add(fallback)

    # Drop duplicates while keeping order.
    unique, seen = [], set()
    for path in candidates:
        key = str(path).lower()
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def find_tesseract_binary(directory: Path) -> Path | None:
    """Locate the executable inside a Tesseract folder."""
    names = ["tesseract.exe", "tesseract"]
    for sub in (directory, directory / "bin"):
        for name in names:
            candidate = sub / name
            if candidate.is_file():
                return candidate
    return None


def find_tessdata(directory: Path) -> Path | None:
    """
    Locate the folder holding the .traineddata language files.

    Tesseract 4 and 5 want TESSDATA_PREFIX pointing at the tessdata folder
    itself, unlike 3.x which wanted its parent, so the folder containing the
    .traineddata files is what gets returned.
    """
    for candidate in (
        directory / "tessdata",
        directory,
        directory / "share" / "tessdata",
        directory / "share" / "tesseract-ocr" / "tessdata",
    ):
        if candidate.is_dir() and any(candidate.glob("*.traineddata")):
            return candidate
    return None


def configure_tesseract(pytesseract, lang: str, quiet: bool = False) -> None:
    """
    Point pytesseract at the project's Tesseract copy.

    Runs once per process. If no bundled copy is found this does nothing and
    Tesseract is looked up on PATH, which is what a system-wide install needs.
    """
    global _TESSERACT_CONFIGURED
    if _TESSERACT_CONFIGURED:
        return
    _TESSERACT_CONFIGURED = True

    # An explicit path that does not exist is always worth reporting, even if
    # another candidate ends up working.
    if TESSERACT_DIR_OVERRIDE:
        explicit = Path(TESSERACT_DIR_OVERRIDE).expanduser()
        resolved = explicit if explicit.is_absolute() else None
        if resolved is not None and not resolved.is_dir() and not quiet:
            print(f"Warning: --tesseract-dir '{TESSERACT_DIR_OVERRIDE}' is not a directory.",
                  file=sys.stderr)

    for directory in candidate_tesseract_dirs():
        if not directory.is_dir():
            continue

        binary = find_tesseract_binary(directory)
        tessdata = find_tessdata(directory)
        if binary is None and tessdata is None:
            continue

        if binary is not None:
            pytesseract.pytesseract.tesseract_cmd = str(binary)

        if not quiet:
            print(f"Tesseract {binary if binary is not None else directory}")

        if tessdata is not None:
            # Set both: the env var covers the binary's own lookup, and the
            # --tessdata-dir argument covers builds that ignore the env var.
            os.environ["TESSDATA_PREFIX"] = str(tessdata)

            available = sorted(p.stem for p in tessdata.glob("*.traineddata"))
            if lang not in available and not quiet:
                print(
                    f"Warning: language '{lang}' not found in {tessdata}.\n"
                    f"         Available: {', '.join(available) or 'none'}",
                    file=sys.stderr,
                )
        return

    # Nothing bundled: fall through to PATH.
    if TESSERACT_DIR_OVERRIDE and not quiet:
        print(
            f"Warning: --tesseract-dir '{TESSERACT_DIR_OVERRIDE}' does not exist; "
            "falling back to PATH.",
            file=sys.stderr,
        )


def tessdata_config_flag() -> str:
    """
    The --tessdata-dir argument for the Tesseract command line, when it is safe
    to pass one.

    pytesseract splits the config string with shlex.split(config, posix=False)
    on Windows, and in non-POSIX mode shlex keeps the quote characters instead
    of consuming them. A quoted path therefore reaches Tesseract as
    '"C:\\path\\tessdata"' and fails to open, while an unquoted path containing
    a space gets split into two arguments. So the flag is only added for
    whitespace-free paths; TESSDATA_PREFIX, set alongside it, is the mechanism
    that covers everything else.
    """
    prefix = os.environ.get("TESSDATA_PREFIX")
    if not prefix or any(ch.isspace() for ch in prefix):
        return ""
    return f" --tessdata-dir {prefix}"


# ============================================
# WORD EXTRACTION
# ============================================

class Word:
    """One token with its bounding box, in whatever units the page uses."""

    __slots__ = ("text", "x1", "x2", "y1", "y2", "conf")

    def __init__(self, text, x1, x2, y1, y2, conf=100.0):
        self.text = text
        self.x1, self.x2, self.y1, self.y2 = x1, x2, y1, y2
        self.conf = conf

    @property
    def cx(self):
        return (self.x1 + self.x2) / 2

    @property
    def cy(self):
        return (self.y1 + self.y2) / 2

    @property
    def height(self):
        return self.y2 - self.y1

    def __repr__(self):
        return f"Word({self.text!r} @ {self.x1:.0f}-{self.x2:.0f}, y={self.y1:.0f})"


def words_from_pdf_page(page) -> list[Word]:
    """Read words and coordinates straight from a PDF text layer."""
    return [
        Word(w["text"], w["x0"], w["x1"], w["top"], w["bottom"])
        for w in page.extract_words(use_text_flow=False, keep_blank_chars=False)
    ]


def words_from_image(image, lang: str, psm: int) -> tuple[list[Word], float]:
    """Run Tesseract and return (word boxes, effective page width in pixels)."""
    try:
        import pytesseract
    except ImportError:
        raise SystemExit(
            "pytesseract is not installed. Run: pip install pytesseract\n"
            "You also need the Tesseract binary itself (see the header of this file)."
        )

    from PIL import Image

    configure_tesseract(pytesseract, lang)

    # Grayscale plus a right-sized upscale is the single biggest accuracy win
    # on screen-resolution statements.
    prepared = image.convert("L")
    scale = min(max(TARGET_OCR_WIDTH / prepared.width, 1.0), MAX_OCR_UPSCALE)
    if scale > 1.05:
        prepared = prepared.resize(
            (round(prepared.width * scale), round(prepared.height * scale)),
            Image.LANCZOS,
        )

    def attempt(config_extra: str):
        return pytesseract.image_to_data(
            prepared,
            lang=lang,
            config=f"--psm {psm}{config_extra}",
            output_type=pytesseract.Output.DICT,
        )

    try:
        data = attempt(tessdata_config_flag())
    except Exception as exc:
        message = str(exc)
        lowered = message.lower()

        if "tesseract" in lowered and ("not installed" in lowered
                                       or "not in your path" in lowered
                                       or "cannot find" in lowered):
            searched = "\n".join(f"  {d}" for d in candidate_tesseract_dirs())
            raise SystemExit(
                f"Could not run Tesseract: {exc}\n\n"
                f"Looked for a Tesseract folder in:\n{searched}\n\n"
                "Expected that folder to contain tesseract.exe and a tessdata subfolder.\n"
                "Point at it explicitly with:\n"
                r'  python local_extraction.py statement.png --tesseract-dir C:\Financial-Dashboard\tesseract'
            )

        if "failed loading language" in lowered or "tessdata" in lowered:
            # Retry with the other TESSDATA_PREFIX convention. Tesseract 4 and 5
            # want the tessdata folder itself; 3.x wanted its parent, and some
            # portable builds follow the old rule.
            prefix = os.environ.get("TESSDATA_PREFIX")
            if prefix and Path(prefix).name.lower() == "tessdata":
                parent = str(Path(prefix).parent)
                os.environ["TESSDATA_PREFIX"] = parent
                try:
                    data = attempt(tessdata_config_flag())
                except Exception:
                    os.environ["TESSDATA_PREFIX"] = prefix  # restore for the message
                else:
                    return finish_ocr(data, prepared)

            prefix = os.environ.get("TESSDATA_PREFIX")
            hint = ""
            if prefix:
                folder = Path(prefix)
                present = sorted(p.name for p in folder.glob("*.traineddata")) if folder.is_dir() else []
                hint = (
                    f"\n{folder} {'exists' if folder.is_dir() else 'does NOT exist'}"
                    f"{', containing: ' + ', '.join(present) if present else ''}"
                )
            raise SystemExit(
                f"Tesseract could not load its language data: {exc}\n"
                f"TESSDATA_PREFIX is {prefix or 'unset'}.{hint}\n"
                f"Make sure {lang}.traineddata sits in that folder."
            )
        raise

    return finish_ocr(data, prepared)


def finish_ocr(data: dict, prepared) -> tuple[list[Word], float]:
    """Turn Tesseract's word table into Word objects."""
    words = []
    for i, text in enumerate(data["text"]):
        text = (text or "").strip()
        if not text:
            continue
        # Filter on content, not confidence. Reverse-video header cells (white
        # text on a dark fill) routinely come back at 1-5% confidence while
        # being read perfectly, and those headers are what label the columns.
        # Discard only table-border glyphs; keep hyphens, which appear inside
        # real descriptions ("Payroll Deposit - HOTEL").
        if all(ch in BORDER_GLYPHS for ch in text):
            continue
        conf = float(data["conf"][i])
        left, top = data["left"][i], data["top"][i]
        words.append(Word(text, left, left + data["width"][i], top, top + data["height"][i], conf))
    return words, float(prepared.width)


def load_pages(path: Path, lang: str, psm: int, force_ocr: bool) -> list[tuple[list[Word], float]]:
    """Return [(words, page_width)] for each page of the input."""
    suffix = path.suffix.lower()

    if suffix != ".pdf":
        from PIL import Image
        image = Image.open(path)
        words, width = words_from_image(image, lang, psm)
        return [(words, width)]

    try:
        import pdfplumber
    except ImportError:
        raise SystemExit("pdfplumber is not installed. Run: pip install pdfplumber")

    pages = []
    with pdfplumber.open(path) as pdf:
        for index, page in enumerate(pdf.pages, start=1):
            words = [] if force_ocr else words_from_pdf_page(page)

            if len(words) < 10:
                # No usable text layer: this page is a scan. Rasterize and OCR.
                reason = "forced" if force_ocr else "no text layer"
                print(f"  page {index}: OCR ({reason})")
                image = page.to_image(resolution=PDF_RASTER_DPI).original
                words, width = words_from_image(image, lang, psm)
                pages.append((words, width))
            else:
                print(f"  page {index}: text layer ({len(words)} words)")
                pages.append((words, float(page.width)))
    return pages


# ============================================
# ROW AND COLUMN GEOMETRY
# ============================================

def group_into_rows(words: list[Word]) -> list[list[Word]]:
    """Cluster words into visual rows by vertical position."""
    if not words:
        return []

    heights = [w.height for w in words if w.height > 0]
    tolerance = statistics.median(heights) * ROW_TOLERANCE if heights else 5.0

    rows: list[list[Word]] = []
    for word in sorted(words, key=lambda w: (w.cy, w.x1)):
        placed = False
        for row in reversed(rows):
            if abs(word.cy - statistics.mean(w.cy for w in row)) <= tolerance:
                row.append(word)
                placed = True
                break
        if placed:
            continue
        rows.append([word])

    for row in rows:
        row.sort(key=lambda w: w.x1)
    rows.sort(key=lambda row: statistics.mean(w.cy for w in row))
    return rows


def row_text(row: list[Word]) -> str:
    return " ".join(w.text for w in row)


def is_currency(token: str) -> bool:
    cleaned = token.replace(" ", "")
    if not any(ch.isdigit() for ch in cleaned):
        return False
    return bool(CURRENCY_RE.match(cleaned))


def cluster_right_edges(values: list[float], tolerance: float) -> list[float]:
    """Group right-edge positions into columns; return their mean positions."""
    clusters: list[list[float]] = []
    for value in sorted(values):
        if clusters and value - clusters[-1][-1] <= tolerance:
            clusters[-1].append(value)
        else:
            clusters.append([value])
    # Require at least two members so a stray token does not invent a column.
    return [statistics.mean(c) for c in clusters if len(c) >= 2]


def find_header_row(rows: list[list[Word]]) -> tuple[int, dict[str, tuple[float, float]]]:
    """
    Locate the table header and map each recognized column to an x-range.
    Returns (row_index, {role: (x1, x2)}). Index is -1 when no header is found.
    """
    best_index, best_score, best_map = -1, 0, {}

    for index, row in enumerate(rows[:40]):  # headers live near the top
        lowered = row_text(row).lower()
        score = sum(
            1 for keywords in HEADER_KEYWORDS.values()
            if any(k in lowered for k in keywords)
        )
        if score < 3 or score <= best_score:
            continue

        column_map: dict[str, tuple[float, float]] = {}
        for word in row:
            token = word.text.lower().strip(" |_-—:")
            for role, keywords in HEADER_KEYWORDS.items():
                if role in column_map:
                    continue
                if any(token.startswith(k) or k.startswith(token) and len(token) > 2 for k in keywords):
                    column_map[role] = (word.x1, word.x2)
                    break

        if len(column_map) >= 3:
            best_index, best_score, best_map = index, score, column_map

    return best_index, best_map


def label_amount_columns(
    centers: list[float],
    header_map: dict[str, tuple[float, float]],
    tolerance: float,
) -> list[str]:
    """
    Decide what each detected amount column means.

    Statements put money columns in a conventional left-to-right order
    (withdrawals, deposits, balance), so position is a stronger signal than
    header text -- header cells are often reverse-video and OCR poorly. Header
    text is used to resolve the ambiguous two-column layouts.
    """
    count = len(centers)
    if count == 0:
        return []

    # Amount-related headers that were read successfully, in x order.
    header_roles = [
        role for role, _ in sorted(
            ((r, b) for r, b in header_map.items() if r in ("withdrawal", "deposit", "balance")),
            key=lambda item: item[1][0],
        )
    ]

    # Header found exactly as many money columns as geometry did: trust it.
    if len(header_roles) == count:
        return header_roles

    if count >= 3:
        labels = ["withdrawal", "deposit", "balance"]
        # Any extra columns to the left are unusual; mark them generically.
        return ["amount"] * (count - 3) + labels

    if count == 2:
        # Either (withdrawal, balance), (deposit, balance) or (amount, balance).
        # A credit card statement with one signed amount column is common.
        if "withdrawal" in header_roles:
            return ["withdrawal", "balance"]
        if "deposit" in header_roles:
            return ["deposit", "balance"]
        return ["amount", "balance"]

    return ["balance"] if "balance" in header_roles else ["amount"]


# ============================================
# VALUE PARSING
# ============================================

def parse_amount(token: str) -> float | None:
    """'1,515.63' -> 1515.63, '(62.47)' -> -62.47, '-72.47' -> -72.47"""
    raw = token.strip()
    if not raw:
        return None

    negative = (raw.startswith("(") and raw.endswith(")")) or raw.startswith("-") or raw.endswith("-")
    cleaned = re.sub(r"[^\d.]", "", raw)
    if not cleaned or cleaned == ".":
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return -value if negative else value


def parse_date(tokens: list[str], default_year: int | None = None) -> tuple[str | None, int]:
    """
    Parse a date from the start of a row.
    Returns (iso_date, tokens_consumed).
    """
    if not tokens:
        return None, 0

    first = tokens[0].strip(" ,")

    # 2003-10-14
    match = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$", first)
    if match:
        y, m, d = (int(g) for g in match.groups())
        return _iso(y, m, d), 1

    # 10/14/2003 or 14/10/03 -- assume month first, the common statement format
    match = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})$", first)
    if match:
        a, b, c = (int(g) for g in match.groups())
        year = c if c > 99 else 2000 + c
        return _iso(year, a, b), 1

    # Oct 14 / Oct 14, 2003 / 14 Oct 2003
    month_key = first.lower().strip(".")[:4]
    month = MONTHS.get(month_key) or MONTHS.get(month_key[:3])
    if month and len(tokens) >= 2:
        day_match = re.match(r"^(\d{1,2})", tokens[1].strip(" ,"))
        if day_match:
            day = int(day_match.group(1))
            year, consumed = default_year, 2
            if len(tokens) >= 3:
                year_match = re.match(r"^(\d{4})$", tokens[2].strip(" ,"))
                if year_match:
                    year, consumed = int(year_match.group(1)), 3
            if year:
                return _iso(year, month, day), consumed
            return None, consumed

    # 14 Oct 2003
    if re.match(r"^\d{1,2}$", first) and len(tokens) >= 2:
        month = MONTHS.get(tokens[1].lower().strip(".")[:3])
        if month:
            year, consumed = default_year, 2
            if len(tokens) >= 3 and re.match(r"^\d{4}$", tokens[2]):
                year, consumed = int(tokens[2]), 3
            if year:
                return _iso(year, month, int(first)), consumed

    return None, 0


def _iso(year: int, month: int, day: int) -> str | None:
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


# ============================================
# TABLE PARSING
# ============================================

def parse_page(words: list[Word], page_width: float, debug: bool = False) -> dict:
    """Pull the transaction table plus summary balances out of one page."""
    rows = group_into_rows(words)
    if not rows:
        return {"transactions": [], "rows_seen": 0}

    tolerance = page_width * COLUMN_TOLERANCE
    header_index, header_map = find_header_row(rows)

    # Candidate data rows: everything below the header (or everything, if no
    # header was found). Rows without a currency token are kept as possible
    # continuation lines for wrapped descriptions, and filtered later.
    start = header_index + 1 if header_index >= 0 else 0
    table_rows = rows[start:]
    candidates = [row for row in table_rows if any(is_currency(w.text) for w in row)]

    right_edges = [w.x2 for row in candidates for w in row if is_currency(w.text)]
    centers = cluster_right_edges(right_edges, tolerance)

    # A column holding only one value on this page produces no cluster, since
    # clustering needs two aligned members to avoid inventing columns from
    # stray tokens. Recover those using the header position: if a money header
    # has at least one currency token beneath it, that is a real column.
    for role in ("withdrawal", "deposit", "balance"):
        if role not in header_map:
            continue
        header_right = header_map[role][1]
        if any(abs(header_right - c) <= tolerance * 1.5 for c in centers):
            continue
        nearby = [e for e in right_edges if abs(e - header_right) <= tolerance * 1.5]
        if nearby:
            centers.append(statistics.mean(nearby))
    centers.sort()

    labels = label_amount_columns(centers, header_map, tolerance)

    if debug:
        print(f"  header row: {header_index} {list(header_map)}")
        print(f"  amount columns: {[(l, round(c)) for l, c in zip(labels, centers)]}")

    # Reference column sits between the description and the first amount column.
    ref_bounds = header_map.get("reference")
    first_amount_x = min(centers) if centers else page_width

    result = {
        "transactions": [],
        "opening_balance": None,
        "closing_balance": None,
        "total_withdrawals": None,
        "total_deposits": None,
        "rows_seen": len(candidates),
        "low_confidence_rows": [],
    }

    description_bounds = header_map.get("description")
    footer_markers = ("page ", "member", "fdic", "equal housing", "continued",
                      "insured", "www.", "customer service", "important")

    for row in table_rows:
        has_currency = any(is_currency(w.text) for w in row)

        # A row with text but no amounts is usually the second line of a
        # wrapped description. Append it to the transaction above, but only if
        # it starts inside the description column and does not look like page
        # furniture.
        if not has_currency:
            if not result["transactions"]:
                continue
            text = re.sub(r"\s+", " ", row_text(row)).strip(" |_-—.")
            lowered_text = text.lower()
            if not text or len(text.split()) > 6:
                continue
            if any(marker in lowered_text for marker in footer_markers):
                continue
            if parse_date([w.text for w in row])[0] is not None:
                continue
            if description_bounds:
                left_edge = min(w.x1 for w in row)
                if not (description_bounds[0] - tolerance <= left_edge <= description_bounds[1] + tolerance * 4):
                    continue
            previous = result["transactions"][-1]
            previous["description"] = f"{previous['description'] or ''} {text}".strip()
            continue

        amounts: dict[str, float] = {}
        leftovers: list[Word] = []

        for word in row:
            if is_currency(word.text) and centers:
                # Snap to the nearest column by right edge.
                distances = [(abs(word.x2 - c), i) for i, c in enumerate(centers)]
                distance, index = min(distances)
                if distance <= tolerance * 2.5:
                    value = parse_amount(word.text)
                    if value is not None:
                        amounts[labels[index]] = value
                        continue
            leftovers.append(word)

        if not amounts:
            continue

        tokens = [w.text for w in leftovers]
        iso_date, consumed = parse_date(tokens)
        remaining = leftovers[consumed:]

        # Reference number: a bare integer sitting in the reference column.
        reference = None
        description_words = []
        for word in remaining:
            in_ref_zone = (
                (ref_bounds and ref_bounds[0] - tolerance <= word.cx <= ref_bounds[1] + tolerance)
                or (not ref_bounds and word.x2 < first_amount_x and word.cx > page_width * 0.55)
            )
            if reference is None and INTEGER_RE.match(word.text) and in_ref_zone:
                reference = word.text
                continue
            description_words.append(word)

        description = " ".join(w.text for w in description_words).strip(" |_-—.")
        description = re.sub(r"\s+", " ", description)
        lowered = description.lower()

        # Route summary rows to their own fields instead of the transaction list.
        if any(marker in lowered for marker in OPENING_MARKERS):
            result["opening_balance"] = amounts.get("balance") or amounts.get("amount")
            continue
        if any(marker in lowered for marker in CLOSING_MARKERS):
            result["closing_balance"] = amounts.get("balance") or amounts.get("amount")
            continue
        if any(marker in lowered.strip("* ") for marker in TOTAL_MARKERS) and iso_date is None:
            result["total_withdrawals"] = amounts.get("withdrawal")
            result["total_deposits"] = amounts.get("deposit")
            continue

        if iso_date is None and not description:
            continue

        # A row with no date but with a description is usually a wrapped
        # description line belonging to the transaction above it.
        if iso_date is None and result["transactions"] and "balance" not in amounts:
            previous = result["transactions"][-1]
            previous["description"] = f"{previous['description']} {description}".strip()
            continue

        transaction = {
            "date": iso_date,
            "description": description or None,
            "reference": reference,
            "withdrawal": amounts.get("withdrawal"),
            "deposit": amounts.get("deposit"),
            "balance": amounts.get("balance"),
        }
        if "amount" in amounts and transaction["withdrawal"] is None and transaction["deposit"] is None:
            transaction["amount"] = amounts["amount"]

        confidences = [w.conf for w in row if w.conf < 100]
        if confidences and statistics.mean(confidences) < 70:
            result["low_confidence_rows"].append(description[:40])

        result["transactions"].append(transaction)

    return result


def parse_metadata(words: list[Word], page_width: float) -> dict:
    """Best-effort header details from the top of the first page."""
    rows = group_into_rows(words)
    header_rows = rows[:20]
    lines = [row_text(row).strip(" |_-—") for row in header_rows]
    joined = " \n".join(lines)

    # Statement headers are two-column: the customer's address block sits left,
    # account and period details right. Reading them separately avoids mixing
    # the two into one meaningless line.
    left_lines = [
        " ".join(w.text for w in row if w.cx < page_width * 0.55).strip(" |_-—")
        for row in header_rows
    ]

    metadata: dict[str, object] = {
        "bank_name": None,
        "account_holder_name": None,
        "account_number": None,
        "account_type": None,
        "statement_period_start": None,
        "statement_period_end": None,
    }

    def clean_line(line: str) -> str:
        # Logos and seals often OCR as one or two stray lowercase letters at the
        # start of the bank's name line.
        tokens = line.split()
        while tokens and len(tokens[0]) <= 2 and tokens[0].islower():
            tokens.pop(0)
        return " ".join(tokens).strip()

    # Bank name: the first substantial mostly-alphabetic line.
    for line in left_lines[:4]:
        candidate = clean_line(line)
        letters = re.sub(r"[^A-Za-z ]", "", candidate).strip()
        if len(letters) >= 8 and len(letters.split()) >= 2:
            metadata["bank_name"] = candidate
            break

    # Account type from the common statement titles.
    for keyword in ("chequing", "checking", "savings", "credit card", "current"):
        if keyword in joined.lower():
            metadata["account_type"] = keyword.title()
            break

    # Account holder: an all-caps name line in the left address block, below
    # the bank's own details.
    title_words = {"statement", "account", "period", "page", "bank", "chequing",
                   "checking", "savings", "box", "street", "st", "ave", "road"}
    for line in left_lines[1:12]:
        candidate = clean_line(line)
        tokens = candidate.split()
        if not (2 <= len(tokens) <= 4):
            continue
        if any(ch.isdigit() for ch in candidate):
            continue
        if candidate != candidate.upper():
            continue
        if any(t.lower().strip(".,") in title_words for t in tokens):
            continue
        if metadata["bank_name"] and candidate in metadata["bank_name"]:
            continue
        metadata["account_holder_name"] = candidate.title()
        break

    # Statement period: two dates joined by 'to' or a dash.
    period = re.search(
        r"(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})\s*(?:to|-|–|through)\s*"
        r"(\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4})",
        joined,
    )
    if period:
        start, _ = parse_date([period.group(1)])
        end, _ = parse_date([period.group(2)])
        metadata["statement_period_start"] = start
        metadata["statement_period_end"] = end

    metadata["account_number"] = find_account_number(header_rows, page_width)
    return metadata


PHONE_RE = re.compile(r"^\+?1?-?\(?\d{3}\)?-\d{3}-\d{4}$|^1-\d{3}-\d{3}-\d{4}$")
DATE_LIKE_RE = re.compile(r"^\d{4}[-/.]\d{1,2}[-/.]\d{1,2}$|^\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}$")


def find_account_number(header_rows: list[list[Word]], page_width: float) -> str | None:
    """
    Find the account number by anchoring to its own column heading.

    Anchoring matters: a bare digit search picks up the bank's toll-free number
    instead. Statements also wrap long account numbers across two lines
    ("00005-" then "123-456-7"), so trailing-dash fragments get stitched.
    """
    def is_fragment(text: str) -> bool:
        text = text.strip()
        if not re.match(r"^\d[\d-]{2,}-?$", text):
            return False
        return not (PHONE_RE.match(text) or DATE_LIKE_RE.match(text))

    # Locate an "Account No./Number/#" heading.
    anchor = None
    for row_index, row in enumerate(header_rows):
        for i, word in enumerate(row):
            if word.text.lower().strip(".:") != "account":
                continue
            following = row[i + 1].text.lower().strip(".:") if i + 1 < len(row) else ""
            if following in ("no", "number", "#", "num") or following.startswith("no"):
                anchor = (row_index, i, word.x1, (row[i + 1].x2 if i + 1 < len(row) else word.x2))
                break
        if anchor:
            break

    if anchor:
        row_index, word_index, ax1, ax2 = anchor
        tolerance = page_width * 0.12

        # The value may sit inline after the label ("Account Number: 4820-99137")
        # or below it in the same column, so check both.
        fragments = [
            w.text.strip() for w in header_rows[row_index][word_index + 1:]
            if is_fragment(w.text)
        ]
        fragments += [
            w.text.strip()
            for row in header_rows[row_index + 1: row_index + 5]
            for w in row
            if is_fragment(w.text) and ax1 - tolerance <= w.cx <= ax2 + tolerance
        ]
    else:
        # No heading found: fall back to any fragment on the page, phone
        # numbers and dates already excluded.
        fragments = [w.text.strip() for row in header_rows for w in row if is_fragment(w.text)]

    stitched, skip_next = [], False
    for i, fragment in enumerate(fragments):
        if skip_next:
            skip_next = False
            continue
        if fragment.endswith("-") and i + 1 < len(fragments):
            stitched.append(fragment + fragments[i + 1])
            skip_next = True
        else:
            stitched.append(fragment)

    for candidate in stitched:
        if sum(ch.isdigit() for ch in candidate) >= 6:
            return candidate
    return stitched[0] if stitched else None


def infer_from_balance_deltas(data: dict) -> list[str]:
    """
    Use the running balance column to settle any ambiguity.

    Every row's balance equals the previous balance plus that row's activity, so
    the delta tells us both the sign (deposit vs withdrawal) and the magnitude.
    This resolves rows whose column assignment was ambiguous and flags rows
    where OCR misread a digit. It is the main advantage of a deterministic
    parser: the document contains enough redundancy to check itself.
    """
    notes: list[str] = []
    transactions = data.get("transactions") or []
    previous = data.get("opening_balance")

    for index, transaction in enumerate(transactions, start=1):
        balance = transaction.get("balance")

        if isinstance(previous, (int, float)) and isinstance(balance, (int, float)):
            delta = round(balance - previous, 2)
            stated = transaction.pop("amount", None)
            magnitude = abs(delta)

            has_split = transaction.get("withdrawal") is not None or transaction.get("deposit") is not None

            if not has_split:
                # Assign the unlabeled amount to the side the delta implies.
                value = stated if stated is not None else magnitude
                if delta > 0:
                    transaction["deposit"] = value
                elif delta < 0:
                    transaction["withdrawal"] = value

                if stated is not None and abs(abs(stated) - magnitude) > 0.01:
                    notes.append(
                        f"Row {index} ({transaction.get('description') or '?'}): amount {stated} "
                        f"does not match the balance change of {magnitude}."
                    )
            else:
                # Both columns were labeled; verify the sign is consistent.
                change = (transaction.get("deposit") or 0) - (transaction.get("withdrawal") or 0)
                if abs(change - delta) > 0.01:
                    notes.append(
                        f"Row {index} ({transaction.get('description') or '?'}): "
                        f"columns imply {change:+.2f} but the balance moved {delta:+.2f}."
                    )
        elif transaction.get("amount") is not None:
            # No balance to compare against; leave the value but keep it visible.
            transaction.setdefault("withdrawal", None)
            transaction.setdefault("deposit", None)

        if isinstance(balance, (int, float)):
            previous = balance

    return notes


# ============================================
# RECONCILIATION
# ============================================

def reconcile(data: dict) -> list[str]:
    """Check the extracted numbers against each other."""
    warnings: list[str] = []
    transactions = data.get("transactions") or []

    if not transactions:
        warnings.append("No transactions were extracted.")
        return warnings

    def summed(field: str) -> float:
        return round(sum(t[field] for t in transactions if isinstance(t.get(field), (int, float))), 2)

    for field, computed, label in (
        ("total_withdrawals", summed("withdrawal"), "withdrawals"),
        ("total_deposits", summed("deposit"), "deposits"),
    ):
        reported = data.get(field)
        if isinstance(reported, (int, float)) and abs(reported - computed) > 0.01:
            warnings.append(f"Sum of {label} ({computed}) does not match reported {field} ({reported}).")

    # The strongest check available: every row carries a running balance, so
    # each step should equal the previous balance plus that row's activity.
    previous = data.get("opening_balance")
    for index, transaction in enumerate(transactions, start=1):
        balance = transaction.get("balance")
        if not isinstance(previous, (int, float)) or not isinstance(balance, (int, float)):
            previous = balance
            continue
        change = (transaction.get("deposit") or 0) - (transaction.get("withdrawal") or 0)
        expected = round(previous + change, 2)
        if abs(expected - balance) > 0.01:
            warnings.append(
                f"Row {index} ({transaction.get('description') or '?'}): balance reads {balance}, "
                f"but {previous} {'+' if change >= 0 else '-'} {abs(change)} = {expected}."
            )
        previous = balance

    if isinstance(previous, (int, float)):
        reported_closing = data.get("closing_balance")
        if reported_closing is None:
            data["closing_balance"] = previous
        elif abs(reported_closing - previous) > 0.01:
            warnings.append(
                f"Final row balance ({previous}) does not match the stated closing "
                f"balance ({reported_closing})."
            )

    if data.get("low_confidence_rows"):
        warnings.append(
            f"Low OCR confidence on {len(data['low_confidence_rows'])} row(s): "
            f"{', '.join(data['low_confidence_rows'][:3])}"
        )

    return warnings


# ============================================
# INPUT DISCOVERY
# ============================================

def expand_inputs(patterns: list[str], use_all: bool) -> list[Path]:
    """
    Turn command-line arguments into a list of statement files.

    Globs are expanded here rather than relying on the shell, because
    PowerShell and cmd.exe pass "*.pdf" through literally.
    """
    if use_all or not patterns:
        found = sorted(
            p for p in Path.cwd().iterdir()
            if p.is_file() and p.suffix.lower() in SUPPORTED_TYPES
        )
        if not found:
            raise SystemExit("No PDF or image files found here. Pass filenames explicitly.")
        if not use_all and len(found) > 1:
            raise SystemExit(
                f"Multiple candidates ({', '.join(p.name for p in found)}).\n"
                "Name the files you want, or pass --all to process every one."
            )
        return found

    resolved: list[Path] = []
    seen: set[Path] = set()

    for pattern in patterns:
        expanded = [Path(m) for m in sorted(Path.cwd().glob(pattern))] if any(
            ch in pattern for ch in "*?["
        ) else []

        if not expanded:
            candidate = Path(pattern).expanduser()
            if not candidate.is_absolute():
                candidate = Path.cwd() / candidate
            # A directory argument means every statement inside it.
            if candidate.is_dir():
                expanded = sorted(
                    p for p in candidate.iterdir()
                    if p.is_file() and p.suffix.lower() in SUPPORTED_TYPES
                )
                if not expanded:
                    print(f"Skipping {pattern}: directory has no supported files", file=sys.stderr)
                    continue
            else:
                expanded = [candidate]

        for path in expanded:
            if not path.exists():
                print(f"Skipping {path.name}: file not found", file=sys.stderr)
                continue
            if path.suffix.lower() not in SUPPORTED_TYPES:
                print(f"Skipping {path.name}: unsupported type '{path.suffix}'", file=sys.stderr)
                continue
            key = path.resolve()
            if key in seen:
                continue
            seen.add(key)
            resolved.append(path)

    if not resolved:
        raise SystemExit("No usable input files.")
    return resolved


def output_path_for(path: Path, args, batch: bool) -> Path:
    """Where one statement's JSON goes."""
    if not batch and args.out:
        target = Path(args.out).expanduser()
    else:
        # Per-file naming keeps a batch from overwriting itself.
        target = Path(f"{path.stem}_transaction_data.json")

    if args.out_dir:
        directory = Path(args.out_dir).expanduser()
        if not directory.is_absolute():
            directory = Path.cwd() / directory
        directory.mkdir(parents=True, exist_ok=True)
        return directory / target.name

    if not target.is_absolute():
        target = Path.cwd() / target
    return target


# ============================================
# EXTRACTION OF ONE STATEMENT
# ============================================

def extract_statement(path: Path, args) -> tuple[dict, list[str]]:
    """Run the full pipeline on one file and return (data, warnings)."""
    pages = load_pages(path, args.lang, args.psm, args.force_ocr)

    combined: dict[str, object] = {
        "transactions": [],
        "opening_balance": None,
        "closing_balance": None,
        "total_withdrawals": None,
        "total_deposits": None,
        "low_confidence_rows": [],
    }

    for index, (words, page_width) in enumerate(pages):
        if args.debug:
            print(f"  --- page {index + 1}: {len(words)} words, width {page_width:.0f} ---")

        if index == 0:
            combined.update({k: v for k, v in parse_metadata(words, page_width).items() if v is not None})

        page_result = parse_page(words, page_width, debug=args.debug)
        combined["transactions"].extend(page_result["transactions"])
        combined["low_confidence_rows"].extend(page_result["low_confidence_rows"])

        for field in ("opening_balance", "closing_balance", "total_withdrawals", "total_deposits"):
            if combined.get(field) is None and page_result.get(field) is not None:
                combined[field] = page_result[field]

    combined["_source_file"] = path.name
    combined["_method"] = "local: pdfplumber/tesseract + geometric column detection"

    warnings = infer_from_balance_deltas(combined) + reconcile(combined)

    if args.debug:
        for transaction in combined["transactions"]:
            print(f"  {transaction}")

    return combined, warnings


# ============================================
# MAIN
# ============================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract bank statement transactions locally, without an LLM.",
        epilog="Examples:\n"
               "  python local_extraction.py statement.pdf\n"
               "  python local_extraction.py jan.pdf feb.pdf mar.png\n"
               '  python local_extraction.py "statements/*.pdf" --combined all.json\n'
               "  python local_extraction.py --all",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("statements", nargs="*", help="One or more files, globs, or a folder.")
    parser.add_argument("-o", "--out", default=None,
                        help=f"Output JSON for a single input (default: {DEFAULT_OUTPUT}). "
                             "Ignored when several files are given.")
    parser.add_argument("--out-dir", default=None, help="Directory for the output files.")
    parser.add_argument("--combined", default=None, metavar="FILE",
                        help="Also write every statement into one JSON array.")
    parser.add_argument("--only-combined", action="store_true",
                        help="Write just the combined file, not one JSON per statement.")
    parser.add_argument("--all", action="store_true",
                        help="Process every supported file in the current folder.")
    parser.add_argument("--lang", default="eng", help="Tesseract language (default: eng).")
    parser.add_argument("--psm", type=int, default=3, help="Tesseract page segmentation mode (default: 3; try 6 or 4).")
    parser.add_argument("--tesseract-dir", default=None, metavar="DIR",
                        help="Folder holding tesseract.exe and tessdata "
                             f"(default: '{TESSERACT_DIR}' beside this script).")
    parser.add_argument("--force-ocr", action="store_true", help="OCR even when the PDF has a text layer.")
    parser.add_argument("--debug", action="store_true", help="Print detected rows and column geometry.")
    args = parser.parse_args()

    global TESSERACT_DIR_OVERRIDE
    TESSERACT_DIR_OVERRIDE = args.tesseract_dir

    if args.only_combined and not args.combined:
        raise SystemExit("--only-combined requires --combined FILE")

    inputs = expand_inputs(args.statements, args.all)
    batch = len(inputs) > 1
    if not batch and args.out is None:
        args.out = DEFAULT_OUTPUT

    print(f"Found    {len(inputs)} file{'s' if batch else ''}")

    results: list[dict] = []
    summary: list[tuple[str, str]] = []
    failures = 0

    for position, path in enumerate(inputs, start=1):
        prefix = f"[{position}/{len(inputs)}] " if batch else ""
        print(f"\n{prefix}Reading  {path.name}")

        try:
            data, warnings = extract_statement(path, args)
        except SystemExit:
            # A missing dependency is fatal for every file, so let it through.
            raise
        except Exception as exc:
            failures += 1
            print(f"Failed   {path.name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            summary.append((path.name, "failed"))
            continue

        results.append(data)
        count = len(data["transactions"])

        if not args.only_combined:
            target = output_path_for(path, args, batch)
            target.write_text(json.dumps(data, indent=2), encoding="utf-8")
            print(f"Wrote    {target} ({count} transaction{'s' if count != 1 else ''})")
        else:
            print(f"Parsed   {count} transaction{'s' if count != 1 else ''}")

        for warning in warnings:
            print(f"Warning: {path.name}: {warning}", file=sys.stderr)

        if not warnings:
            print("Checks   balances reconcile cleanly")

        summary.append((path.name, f"{count} txns" + (f", {len(warnings)} warning(s)" if warnings else ", clean")))

    if args.combined and results:
        combined_path = Path(args.combined).expanduser()
        if args.out_dir and not combined_path.is_absolute():
            combined_path = Path(args.out_dir).expanduser() / combined_path
        if not combined_path.is_absolute():
            combined_path = Path.cwd() / combined_path
        combined_path.parent.mkdir(parents=True, exist_ok=True)
        combined_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
        total = sum(len(r["transactions"]) for r in results)
        print(f"\nWrote    {combined_path} ({len(results)} statements, {total} transactions)")

    if batch:
        print("\nSummary")
        width = max(len(name) for name, _ in summary)
        for name, status in summary:
            print(f"  {name.ljust(width)}  {status}")
        if failures:
            print(f"  {failures} file(s) failed", file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())