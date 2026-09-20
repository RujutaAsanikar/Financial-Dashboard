# WORKS WITH ONE IF TESSERACT IS IN THE MAIN BRANCH AND NOT ITS OWN FOLDER.
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
Plus the Tesseract binary:
    Windows: https://github.com/UB-Mannheim/tesseract/wiki
    macOS:   brew install tesseract
    Linux:   sudo apt install tesseract-ocr
(Only needed for images and scanned PDFs. Text-layer PDFs need neither.)
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

# ============================================
# CONFIGURATION
# ============================================

DEFAULT_OUTPUT = "transaction_data.json"

SUPPORTED_TYPES = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}

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

    # Grayscale plus a right-sized upscale is the single biggest accuracy win
    # on screen-resolution statements.
    prepared = image.convert("L")
    scale = min(max(TARGET_OCR_WIDTH / prepared.width, 1.0), MAX_OCR_UPSCALE)
    if scale > 1.05:
        prepared = prepared.resize(
            (round(prepared.width * scale), round(prepared.height * scale)),
            Image.LANCZOS,
        )

    try:
        data = pytesseract.image_to_data(
            prepared,
            lang=lang,
            config=f"--psm {psm}",
            output_type=pytesseract.Output.DICT,
        )
    except Exception as exc:
        if "tesseract" in str(exc).lower():
            raise SystemExit(
                f"Could not run Tesseract: {exc}\n"
                "Install it, or if it is installed but not on PATH, set:\n"
                '  pytesseract.pytesseract.tesseract_cmd = r"C:\\Program Files\\Tesseract-OCR\\tesseract.exe"'
            )
        raise

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

def resolve_input(arg: str | None) -> Path:
    if arg is None:
        candidates = sorted(
            p for p in Path.cwd().iterdir()
            if p.is_file() and p.suffix.lower() in SUPPORTED_TYPES
        )
        if not candidates:
            raise SystemExit("No PDF or image files found here. Pass a filename explicitly.")
        if len(candidates) > 1:
            raise SystemExit(f"Multiple candidates ({', '.join(p.name for p in candidates)}). Pass one explicitly.")
        return candidates[0]

    path = Path(arg).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.exists():
        raise SystemExit(f"File not found: {path}")
    if path.suffix.lower() not in SUPPORTED_TYPES:
        raise SystemExit(f"Unsupported type '{path.suffix}'. Supported: {', '.join(sorted(SUPPORTED_TYPES))}")
    return path


# ============================================
# MAIN
# ============================================

def main() -> int:
    parser = argparse.ArgumentParser(description="Extract bank statement transactions locally, without an LLM.")
    parser.add_argument("statement", nargs="?", help="Statement file (PDF or image).")
    parser.add_argument("-o", "--out", default=DEFAULT_OUTPUT, help=f"Output JSON (default: {DEFAULT_OUTPUT}).")
    parser.add_argument("--lang", default="eng", help="Tesseract language (default: eng).")
    parser.add_argument("--psm", type=int, default=3, help="Tesseract page segmentation mode (default: 3; try 6 or 4).")
    parser.add_argument("--force-ocr", action="store_true", help="OCR even when the PDF has a text layer.")
    parser.add_argument("--debug", action="store_true", help="Print detected rows and column geometry.")
    args = parser.parse_args()

    statement_path = resolve_input(args.statement)
    output_path = Path(args.out).expanduser()
    if not output_path.is_absolute():
        output_path = Path.cwd() / output_path

    print(f"Reading  {statement_path.name}")
    pages = load_pages(statement_path, args.lang, args.psm, args.force_ocr)

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

    combined["_source_file"] = statement_path.name
    combined["_method"] = "local: pdfplumber/tesseract + geometric column detection"

    inference_notes = infer_from_balance_deltas(combined)
    warnings = inference_notes + reconcile(combined)

    if args.debug:
        for transaction in combined["transactions"]:
            print(f"  {transaction}")

    output_path.write_text(json.dumps(combined, indent=2), encoding="utf-8")

    count = len(combined["transactions"])
    print(f"Wrote    {output_path} ({count} transaction{'s' if count != 1 else ''})")

    for warning in warnings:
        print(f"Warning: {warning}", file=sys.stderr)

    if not warnings:
        print("Checks   balances reconcile cleanly")

    return 0


if __name__ == "__main__":
    sys.exit(main())