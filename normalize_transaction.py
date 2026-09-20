#!/usr/bin/env python3
"""
Normalize extracted bank statement data into a fixed schema.

Reads transaction_data.json (from either data_extraction.py or
local_extraction.py) and writes a file with exactly these keys, in this order,
using null for anything the input did not provide:

    bank_name, account_holder_name, account_number, account_type,
    statement_period_start, statement_period_end, currency,
    opening_balance, closing_balance,
    transactions[{date, description, reference, withdrawal, deposit, balance}],
    total_withdrawals, total_deposits

Usage:
    python normalize_statement.py
    python normalize_statement.py transaction_data.json -o statement_clean.json
    python normalize_statement.py --derive --currency CAD
    python normalize_statement.py --upper-names

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DEFAULT_INPUT = "transaction_data.json"
DEFAULT_OUTPUT = "statement_normalized.json"

# Target schema. Order here is the order written to the output file.
TOP_LEVEL_FIELDS = [
    "bank_name",
    "account_holder_name",
    "account_number",
    "account_type",
    "statement_period_start",
    "statement_period_end",
    "currency",
    "opening_balance",
    "closing_balance",
    "transactions",
    "total_withdrawals",
    "total_deposits",
]

TRANSACTION_FIELDS = ["date", "description", "reference", "withdrawal", "deposit", "balance"]

# Alternative names the two extractors (and other tools) might use. The first
# match wins, so canonical names are listed first.
TOP_LEVEL_ALIASES = {
    "bank_name": ["bank_name", "bank", "institution", "institution_name", "vendor_name"],
    "account_holder_name": ["account_holder_name", "account_holder", "holder_name", "customer_name", "name"],
    "account_number": ["account_number", "account_no", "account", "acct_number", "acct_no"],
    "account_type": ["account_type", "type", "product", "product_type"],
    "statement_period_start": [
        "statement_period_start", "period_start", "start_date", "from_date",
        "statement_start", "statement_start_date",
    ],
    "statement_period_end": [
        "statement_period_end", "period_end", "end_date", "to_date",
        "statement_end", "statement_end_date",
    ],
    "currency": ["currency", "currency_code", "iso_currency"],
    "opening_balance": ["opening_balance", "previous_balance", "beginning_balance", "balance_forward", "start_balance"],
    "closing_balance": ["closing_balance", "ending_balance", "new_balance", "final_balance", "end_balance"],
    "total_withdrawals": ["total_withdrawals", "total_debits", "withdrawals_total", "total_paid_out", "total_withdrawal"],
    "total_deposits": ["total_deposits", "total_credits", "deposits_total", "total_paid_in", "total_deposit"],
}

TRANSACTION_ALIASES = {
    "date": ["date", "transaction_date", "posted_date", "post_date", "value_date", "effective_date"],
    "description": ["description", "details", "particulars", "narrative", "memo", "merchant", "payee", "transaction"],
    "reference": ["reference", "ref", "ref_no", "cheque_number", "check_number", "check_no", "confirmation"],
    "withdrawal": ["withdrawal", "withdrawals", "debit", "debits", "paid_out", "money_out", "amount_debit"],
    "deposit": ["deposit", "deposits", "credit", "credits", "paid_in", "money_in", "amount_credit"],
    "balance": ["balance", "running_balance", "balance_after", "closing_balance"],
}

# Nested containers some extractors use for the statement period.
PERIOD_CONTAINERS = ["statement_period", "period", "statement_dates"]
PERIOD_START_KEYS = ["start", "start_date", "from", "begin", "beginning"]
PERIOD_END_KEYS = ["end", "end_date", "to", "finish", "ending"]

# A single signed amount column, seen on credit card statements.
SIGNED_AMOUNT_ALIASES = ["amount", "transaction_amount", "value", "net_amount"]

TRANSACTION_LIST_KEYS = ["transactions", "transaction", "lines", "entries", "rows", "activity", "items"]

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

CURRENCY_SYMBOLS = {"$": None, "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR"}


# ============================================
# VALUE COERCION
# ============================================

def pick(source: dict, names: list[str]):
    """Return the first present, non-empty value among these key names."""
    if not isinstance(source, dict):
        return None

    lowered = {str(k).strip().lower(): v for k, v in source.items()}
    for name in names:
        if name in lowered:
            value = lowered[name]
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            return value
    return None


def to_number(value) -> float | None:
    """'$1,515.63' -> 1515.63, '(62.47)' -> -62.47, 1.5 -> 1.5, '' -> None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None

    raw = value.strip()
    if not raw or raw.lower() in ("null", "none", "n/a", "na", "-", "--"):
        return None

    negative = (raw.startswith("(") and raw.endswith(")")) or raw.startswith("-") or raw.endswith("-")
    # Keep digits, dot and comma, then treat comma as a thousands separator
    # unless it is clearly a decimal comma (e.g. European "1.234,56").
    cleaned = re.sub(r"[^\d.,]", "", raw)
    if not cleaned:
        return None

    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        parts = cleaned.split(",")
        # "1234,56" is a decimal comma; "1,234" is a thousands separator.
        cleaned = cleaned.replace(",", ".") if len(parts[-1]) == 2 and len(parts) == 2 else cleaned.replace(",", "")

    try:
        number = float(cleaned)
    except ValueError:
        return None
    return -abs(number) if negative else number


def to_iso_date(value) -> str | None:
    """Normalize common date spellings to YYYY-MM-DD; None if unparseable."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return None

    raw = str(value).strip()
    if not raw or raw.lower() in ("null", "none", "n/a", "na"):
        return None

    # Already ISO
    match = re.match(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", raw)
    if match:
        return _iso(*(int(g) for g in match.groups()))

    # 10/14/2024 or 14/10/24 -- month first, the common statement convention
    match = re.match(r"^(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})$", raw)
    if match:
        a, b, c = (int(g) for g in match.groups())
        year = c if c > 99 else 2000 + c
        if a > 12 >= b:  # unambiguously day-first
            return _iso(year, b, a)
        return _iso(year, a, b)

    # 14 Oct 2003 / Oct 14, 2003 / October 14 2003
    match = re.match(r"^(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{2,4})$", raw)
    if match:
        day, month_name, year = match.groups()
        month = MONTHS.get(month_name.lower()[:4]) or MONTHS.get(month_name.lower()[:3])
        if month:
            year_int = int(year)
            return _iso(year_int if year_int > 99 else 2000 + year_int, month, int(day))

    match = re.match(r"^([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(\d{2,4})$", raw)
    if match:
        month_name, day, year = match.groups()
        month = MONTHS.get(month_name.lower()[:4]) or MONTHS.get(month_name.lower()[:3])
        if month:
            year_int = int(year)
            return _iso(year_int if year_int > 99 else 2000 + year_int, month, int(day))

    return None


def _iso(year: int, month: int, day: int) -> str | None:
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def to_text(value) -> str | None:
    """Collapse whitespace; empty and placeholder strings become None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        joined = " ".join(str(v) for v in value if v is not None)
        return to_text(joined)
    if not isinstance(value, str):
        return None

    text = re.sub(r"\s+", " ", value).strip()
    if not text or text.lower() in ("null", "none", "n/a", "na", "-", "--", "unknown"):
        return None
    return text


def to_reference(value) -> str | None:
    """References stay strings so leading zeros survive ('0064')."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return to_text(value)


# ============================================
# INPUT SHAPE HANDLING
# ============================================

def load_input(path: Path) -> dict:
    """Read the file and reduce it to a single statement dict."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"Input file not found: {path}")
    except json.JSONDecodeError as err:
        raise SystemExit(f"{path.name} is not valid JSON: {err}")

    # A per-page list (what data_extraction.py writes for multi-page input)
    if isinstance(raw, list):
        pages = [p for p in raw if isinstance(p, dict)]
        if not pages:
            raise SystemExit(f"{path.name} contains a list with no statement objects.")
        return merge_pages(pages)

    if not isinstance(raw, dict):
        raise SystemExit(f"{path.name} must contain a JSON object or a list of objects.")

    # Sometimes the statement is nested one level down.
    for wrapper in ("statement", "data", "result", "extracted_data"):
        inner = raw.get(wrapper)
        if isinstance(inner, dict) and find_transaction_list(inner) is not None:
            return inner

    return raw


def find_transaction_list(source: dict) -> list | None:
    lowered = {str(k).strip().lower(): v for k, v in source.items()}
    for key in TRANSACTION_LIST_KEYS:
        value = lowered.get(key)
        if isinstance(value, list):
            return value
    return None


def merge_pages(pages: list[dict]) -> dict:
    """Combine per-page objects: concatenate transactions, first-seen metadata."""
    merged: dict = {}
    transactions: list = []

    for page in pages:
        page_transactions = find_transaction_list(page) or []
        transactions.extend(t for t in page_transactions if isinstance(t, dict))
        for key, value in page.items():
            if key in TRANSACTION_LIST_KEYS:
                continue
            if merged.get(key) in (None, "", []) and value not in (None, "", []):
                merged[key] = value

    merged["transactions"] = transactions
    return merged


# ============================================
# NORMALIZATION
# ============================================

def normalize_transaction(source: dict) -> dict:
    """Map one input row onto the six target transaction fields."""
    row = {field: None for field in TRANSACTION_FIELDS}

    row["date"] = to_iso_date(pick(source, TRANSACTION_ALIASES["date"]))
    row["description"] = to_text(pick(source, TRANSACTION_ALIASES["description"]))
    row["reference"] = to_reference(pick(source, TRANSACTION_ALIASES["reference"]))
    row["balance"] = to_number(pick(source, TRANSACTION_ALIASES["balance"]))

    withdrawal = to_number(pick(source, TRANSACTION_ALIASES["withdrawal"]))
    deposit = to_number(pick(source, TRANSACTION_ALIASES["deposit"]))

    # A single signed amount column splits into the two target columns.
    if withdrawal is None and deposit is None:
        signed = to_number(pick(source, SIGNED_AMOUNT_ALIASES))
        if signed is not None:
            if signed < 0:
                withdrawal = abs(signed)
            elif signed > 0:
                deposit = signed

    # Both columns are magnitudes in the target schema.
    row["withdrawal"] = abs(withdrawal) if withdrawal is not None else None
    row["deposit"] = abs(deposit) if deposit is not None else None

    return row


def infer_currency(source: dict) -> str | None:
    """Read a currency code, or map a symbol found in the raw values."""
    stated = to_text(pick(source, TOP_LEVEL_ALIASES["currency"]))
    if stated:
        match = re.search(r"\b([A-Z]{3})\b", stated.upper())
        if match:
            return match.group(1)
        for symbol, code in CURRENCY_SYMBOLS.items():
            if symbol in stated and code:
                return code
    return None


def normalize(source: dict, derive: bool, currency_override: str | None, upper_names: bool) -> tuple[dict, list[str]]:
    """Produce the target object plus a list of fields left null."""
    result: dict = {}

    result["bank_name"] = to_text(pick(source, TOP_LEVEL_ALIASES["bank_name"]))
    result["account_holder_name"] = to_text(pick(source, TOP_LEVEL_ALIASES["account_holder_name"]))
    result["account_number"] = to_text(pick(source, TOP_LEVEL_ALIASES["account_number"]))
    result["account_type"] = to_text(pick(source, TOP_LEVEL_ALIASES["account_type"]))

    if upper_names:
        for field in ("bank_name", "account_holder_name"):
            if result[field]:
                result[field] = result[field].upper()

    start = pick(source, TOP_LEVEL_ALIASES["statement_period_start"])
    end = pick(source, TOP_LEVEL_ALIASES["statement_period_end"])

    # Fall back to a nested period object or a "X to Y" string.
    if start is None or end is None:
        for container_key in PERIOD_CONTAINERS:
            container = pick(source, [container_key])
            if isinstance(container, dict):
                start = start if start is not None else pick(container, PERIOD_START_KEYS)
                end = end if end is not None else pick(container, PERIOD_END_KEYS)
                break
            if isinstance(container, str):
                parts = re.split(r"\s+(?:to|through|-|–|—)\s+", container)
                if len(parts) == 2:
                    start = start if start is not None else parts[0]
                    end = end if end is not None else parts[1]
                break

    result["statement_period_start"] = to_iso_date(start)
    result["statement_period_end"] = to_iso_date(end)
    result["currency"] = (currency_override or infer_currency(source) or None)

    result["opening_balance"] = to_number(pick(source, TOP_LEVEL_ALIASES["opening_balance"]))
    result["closing_balance"] = to_number(pick(source, TOP_LEVEL_ALIASES["closing_balance"]))

    raw_transactions = find_transaction_list(source) or []
    result["transactions"] = [
        normalize_transaction(t) for t in raw_transactions if isinstance(t, dict)
    ]

    result["total_withdrawals"] = to_number(pick(source, TOP_LEVEL_ALIASES["total_withdrawals"]))
    result["total_deposits"] = to_number(pick(source, TOP_LEVEL_ALIASES["total_deposits"]))

    if derive:
        fill_derivable(result)

    # Fixed key order, nulls for anything still missing.
    ordered = {field: result.get(field) for field in TOP_LEVEL_FIELDS}
    missing = [
        field for field in TOP_LEVEL_FIELDS
        if field != "transactions" and ordered.get(field) is None
    ]
    return ordered, missing


def fill_derivable(result: dict) -> None:
    """Compute totals and closing balance from the rows, when absent."""
    transactions = result.get("transactions") or []
    if not transactions:
        return

    def column_sum(field: str) -> float:
        return round(sum(t[field] for t in transactions if isinstance(t.get(field), (int, float))), 2)

    if result.get("total_withdrawals") is None:
        result["total_withdrawals"] = column_sum("withdrawal")
    if result.get("total_deposits") is None:
        result["total_deposits"] = column_sum("deposit")

    if result.get("closing_balance") is None:
        for transaction in reversed(transactions):
            if isinstance(transaction.get("balance"), (int, float)):
                result["closing_balance"] = transaction["balance"]
                break

    if result.get("opening_balance") is None:
        first = transactions[0]
        if isinstance(first.get("balance"), (int, float)):
            change = (first.get("deposit") or 0) - (first.get("withdrawal") or 0)
            result["opening_balance"] = round(first["balance"] - change, 2)

    # Period dates from the transaction range.
    dates = sorted(t["date"] for t in transactions if t.get("date"))
    if dates:
        if result.get("statement_period_start") is None:
            result["statement_period_start"] = dates[0]
        if result.get("statement_period_end") is None:
            result["statement_period_end"] = dates[-1]


# ============================================
# MAIN
# ============================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Normalize extracted statement JSON into the fixed target schema."
    )
    parser.add_argument("input", nargs="?", default=DEFAULT_INPUT,
                        help=f"Input JSON (default: {DEFAULT_INPUT}).")
    parser.add_argument("-o", "--out", default=DEFAULT_OUTPUT,
                        help=f"Output JSON (default: {DEFAULT_OUTPUT}).")
    parser.add_argument("--derive", action="store_true",
                        help="Compute missing totals, balances and period dates from the rows "
                             "instead of leaving them null.")
    parser.add_argument("--currency", default=None,
                        help="Set the currency code (it is rarely printed on statements).")
    parser.add_argument("--upper-names", action="store_true",
                        help="Uppercase bank_name and account_holder_name.")
    parser.add_argument("--in-place", action="store_true",
                        help="Overwrite the input file with the normalized output.")
    args = parser.parse_args()

    input_path = Path(args.input).expanduser()
    if not input_path.is_absolute():
        input_path = Path.cwd() / input_path

    output_path = input_path if args.in_place else Path(args.out).expanduser()
    if not output_path.is_absolute():
        output_path = Path.cwd() / output_path

    source = load_input(input_path)
    normalized, missing = normalize(
        source,
        derive=args.derive,
        currency_override=args.currency.upper() if args.currency else None,
        upper_names=args.upper_names,
    )

    output_path.write_text(json.dumps(normalized, indent=2) + "\n", encoding="utf-8")

    count = len(normalized["transactions"])
    print(f"Read     {input_path.name}")
    print(f"Wrote    {output_path} ({count} transaction{'s' if count != 1 else ''})")

    # Report rows with gaps, so a null is a decision rather than a surprise.
    incomplete = sum(
        1 for t in normalized["transactions"]
        if t["date"] is None or (t["withdrawal"] is None and t["deposit"] is None)
    )
    if incomplete:
        print(f"Note     {incomplete} transaction(s) missing a date or an amount", file=sys.stderr)

    if missing:
        print(f"Null     {', '.join(missing)}", file=sys.stderr)
    else:
        print("Fields   all top-level fields populated")

    return 0


if __name__ == "__main__":
    sys.exit(main())