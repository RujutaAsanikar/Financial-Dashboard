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
    "apr",
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
    "apr": ["apr", "annual_percentage_rate", "interest_rate"],
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

def load_input(path: Path, list_as: str = "pages") -> list[dict]:
    """
    Read one file and return a list of statement dicts.

    A JSON array is ambiguous: data_extraction.py writes one entry per *page*
    of a single statement, but a combined batch file holds one entry per
    *statement*. list_as decides, with 'auto' guessing from the contents.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"Input file not found: {path}")
    except json.JSONDecodeError as err:
        raise SystemExit(f"{path.name} is not valid JSON: {err}")

    if isinstance(raw, list):
        entries = [p for p in raw if isinstance(p, dict)]
        if not entries:
            raise SystemExit(f"{path.name} contains a list with no statement objects.")

        mode = list_as
        if mode == "auto":
            mode = "statements" if looks_like_separate_statements(entries) else "pages"

        if mode == "statements":
            return [unwrap(entry) for entry in entries]
        return [merge_pages(entries)]

    if not isinstance(raw, dict):
        raise SystemExit(f"{path.name} must contain a JSON object or a list of objects.")

    return [unwrap(raw)]


def unwrap(source: dict) -> dict:
    """Descend through a wrapper key if the statement is nested one level down."""
    for wrapper in ("statement", "data", "result", "extracted_data"):
        inner = source.get(wrapper)
        if isinstance(inner, dict) and find_transaction_list(inner) is not None:
            return inner
    return source


def looks_like_separate_statements(entries: list[dict]) -> bool:
    """
    Guess whether array entries are distinct statements rather than pages.

    Pages of one statement share (or omit) the account identity and the header
    metadata appears only on the first entry. Separate statements each carry
    their own identity, or differ in account number.
    """
    if len(entries) < 2:
        return False

    identity_keys = ("account_number", "acct_no", "account_no", "_source_file", "bank_name", "institution")

    def identity(entry: dict) -> tuple:
        lowered = {str(k).strip().lower(): v for k, v in entry.items()}
        return tuple(str(lowered.get(k)) for k in identity_keys if lowered.get(k) is not None)

    identities = [identity(entry) for entry in entries]
    if any(not i for i in identities):
        return False  # at least one entry has no identity: page-like
    return len(set(identities)) > 1 or all(identities)


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
# INPUT DISCOVERY
# ============================================

def expand_inputs(patterns: list[str], use_all: bool) -> list[Path]:
    """
    Turn arguments into a list of JSON files.

    Globs are expanded here because PowerShell and cmd.exe pass "*.json"
    through literally.
    """
    def is_candidate(path: Path) -> bool:
        # Skip our own outputs so repeated runs do not pile up.
        return path.suffix.lower() == ".json" and not path.stem.endswith("_normalized")

    if use_all or not patterns:
        found = sorted(p for p in Path.cwd().iterdir() if p.is_file() and is_candidate(p))
        if not found:
            raise SystemExit("No JSON files found here. Pass filenames explicitly.")
        if not use_all:
            default = Path.cwd() / DEFAULT_INPUT
            if default.exists():
                return [default]
            if len(found) > 1:
                raise SystemExit(
                    f"Multiple JSON files ({', '.join(p.name for p in found)}).\n"
                    "Name the files you want, or pass --all to normalize every one."
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
            if candidate.is_dir():
                expanded = sorted(p for p in candidate.iterdir() if p.is_file() and is_candidate(p))
                if not expanded:
                    print(f"Skipping {pattern}: directory has no JSON files", file=sys.stderr)
                    continue
            else:
                expanded = [candidate]

        for path in expanded:
            if not path.exists():
                print(f"Skipping {path.name}: file not found", file=sys.stderr)
                continue
            key = path.resolve()
            if key in seen:
                continue
            seen.add(key)
            resolved.append(path)

    if not resolved:
        raise SystemExit("No usable input files.")
    return resolved


def output_path_for(path: Path, args, batch: bool, suffix: str = "") -> Path:
    """Where one normalized statement goes."""
    if args.in_place:
        return path

    if not batch and args.out:
        target = Path(args.out).expanduser()
        # A single input file can still hold several statements; keep them apart.
        if suffix:
            target = target.with_name(f"{target.stem}{suffix}{target.suffix}")
    else:
        target = Path(f"{path.stem}{suffix}_normalized.json")

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
# MAIN
# ============================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Normalize extracted statement JSON into the fixed target schema.",
        epilog="Examples:\n"
               "  python normalize_statement.py\n"
               "  python normalize_statement.py jan.json feb.json mar.json\n"
               '  python normalize_statement.py "*_transaction_data.json" --combined clean.json\n'
               "  python normalize_statement.py --all --derive --currency CAD",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("inputs", nargs="*",
                        help=f"One or more JSON files, globs, or a folder (default: {DEFAULT_INPUT}).")
    parser.add_argument("-o", "--out", default=None,
                        help=f"Output path for a single input (default: {DEFAULT_OUTPUT}). "
                             "Ignored when several files are given.")
    parser.add_argument("--out-dir", default=None, help="Directory for the output files.")
    parser.add_argument("--combined", default=None, metavar="FILE",
                        help="Also write every normalized statement into one JSON array.")
    parser.add_argument("--only-combined", action="store_true",
                        help="Write just the combined file, not one JSON per statement.")
    parser.add_argument("--all", action="store_true",
                        help="Normalize every JSON file in the current folder.")
    parser.add_argument("--list-as", choices=["pages", "statements", "auto"], default="auto",
                        help="How to read a JSON array: pages of one statement, separate "
                             "statements, or guess (default: auto).")
    parser.add_argument("--derive", action="store_true",
                        help="Compute missing totals, balances and period dates from the rows "
                             "instead of leaving them null.")
    parser.add_argument("--currency", default=None,
                        help="Set the currency code (it is rarely printed on statements).")
    parser.add_argument("--upper-names", action="store_true",
                        help="Uppercase bank_name and account_holder_name.")
    parser.add_argument("--in-place", action="store_true",
                        help="Overwrite each input file with its normalized output.")
    args = parser.parse_args()

    if args.only_combined and not args.combined:
        raise SystemExit("--only-combined requires --combined FILE")
    if args.in_place and args.only_combined:
        raise SystemExit("--in-place and --only-combined are contradictory")

    inputs = expand_inputs(args.inputs, args.all)
    batch = len(inputs) > 1
    if not batch and args.out is None and not args.in_place:
        args.out = DEFAULT_OUTPUT

    if batch:
        print(f"Found    {len(inputs)} file{'s' if batch else ''}")

    all_normalized: list[dict] = []
    summary: list[tuple[str, str]] = []
    failures = 0

    for path in inputs:
        try:
            statements = load_input(path, args.list_as)
        except SystemExit as exc:
            failures += 1
            print(f"Failed   {exc}", file=sys.stderr)
            summary.append((path.name, "failed"))
            continue

        # One input file can hold several statements (a combined batch file).
        multiple_inside = len(statements) > 1
        file_normalized: list[dict] = []
        file_missing: set[str] = set()

        for source in statements:
            normalized, missing = normalize(
                source,
                derive=args.derive,
                currency_override=args.currency.upper() if args.currency else None,
                upper_names=args.upper_names,
            )
            file_normalized.append(normalized)
            file_missing.update(missing)

        all_normalized.extend(file_normalized)

        if not args.only_combined:
            if args.in_place:
                # Preserve the input's shape: an array file stays an array.
                payload = file_normalized if multiple_inside else file_normalized[0]
                path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
                total_here = sum(len(n["transactions"]) for n in file_normalized)
                print(f"Wrote    {path} ({total_here} transaction{'s' if total_here != 1 else ''})")
            else:
                for position, normalized in enumerate(file_normalized, start=1):
                    suffix = f"_{position}" if multiple_inside else ""
                    target = output_path_for(path, args, batch, suffix)
                    target.write_text(json.dumps(normalized, indent=2) + "\n", encoding="utf-8")
                    count = len(normalized["transactions"])
                    print(f"Wrote    {target} ({count} transaction{'s' if count != 1 else ''})")

        total = sum(len(n["transactions"]) for n in file_normalized)
        label = f"{len(statements)} statements, {total} txns" if multiple_inside else f"{total} txns"
        summary.append((path.name, label))

        if not batch:
            print(f"Read     {path.name}")
            incomplete = sum(
                1 for n in file_normalized for t in n["transactions"]
                if t["date"] is None or (t["withdrawal"] is None and t["deposit"] is None)
            )
            if incomplete:
                print(f"Note     {incomplete} transaction(s) missing a date or an amount", file=sys.stderr)
            if file_missing:
                print(f"Null     {', '.join(sorted(file_missing))}", file=sys.stderr)
            else:
                print("Fields   all top-level fields populated")
        elif file_missing:
            print(f"Null     {path.name}: {', '.join(sorted(file_missing))}", file=sys.stderr)

    if args.combined and all_normalized:
        combined_path = Path(args.combined).expanduser()
        if args.out_dir and not combined_path.is_absolute():
            combined_path = Path(args.out_dir).expanduser() / combined_path
        if not combined_path.is_absolute():
            combined_path = Path.cwd() / combined_path
        combined_path.parent.mkdir(parents=True, exist_ok=True)
        combined_path.write_text(json.dumps(all_normalized, indent=2) + "\n", encoding="utf-8")
        total = sum(len(n["transactions"]) for n in all_normalized)
        print(f"Wrote    {combined_path} ({len(all_normalized)} statements, {total} transactions)")

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