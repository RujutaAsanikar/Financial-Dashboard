#!/usr/bin/env python3
"""
Extract structured transaction data from a bank statement using Google's Gemini API.

Usage:
    python data_extraction.py statement.pdf
    python data_extraction.py statement.png --out my_data.json
    python data_extraction.py                  # auto-detects a single statement file

Writes transaction_data.json to the current working directory.

Requires: pip install google-genai
Set your key via the API_KEY constant below, or the GEMINI_API_KEY env var.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from google import genai
from google.genai import types

# import socket

# # Save the original getaddrinfo
# original_getaddrinfo = socket.getaddrinfo

# def forced_dns_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
#     # If the app tries to reach the blocked Gemini endpoint, intercept it
#     if host == "://googleapis.com":
#         # Force it to resolve to one of the valid IPs you found via 8.8.8.8
#         host = "172.217.113.4" 
#     return original_getaddrinfo(host, port, family, type, proto, flags)

# # Override the global socket behavior for your script
# socket.getaddrinfo = forced_dns_getaddrinfo

# ============================================
# CONFIGURATION
# ============================================

# Paste your Gemini API key here, or leave blank and set GEMINI_API_KEY in your
# environment (the --api-key flag overrides both).
API_KEY = "AQ.Ab8RN6KTDv-x1xR7rfQQMUcgIgwnloGTOYm7XAuvIuRq8IPQpA"

# Gemini's current general-purpose multimodal model.
MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

DEFAULT_OUTPUT = "transaction_data.json"

# Files under this size are sent inline; larger ones go through the Files API.
INLINE_LIMIT_BYTES = 15 * 1024 * 1024

SUPPORTED_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}

SYSTEM_INSTRUCTION = (
    "You are an expert assistant specialized in extracting structured information "
    "from banking documents such as chequing account statements, savings statements, "
    "and credit card statements. Read every row of the transaction table carefully, "
    "preserve the order in which rows appear, and never invent a value you cannot see. "
    "Return the extracted data strictly in JSON format."
)

EXTRACTION_PROMPT = """
Extract the following information from this bank statement and structure it as a
single JSON object. The document may span multiple pages; treat it as one statement
and return one combined object.

- `bank_name`: The name of the bank or financial institution.
- `bank_address`: The bank's address as printed on the statement (if available).
- `account_holder_name`: The name of the account holder.
- `account_holder_address`: The mailing address of the account holder (if available).
- `account_number`: The account number exactly as shown (keep any dashes or masking).
- `account_type`: The type of account (e.g. 'Chequing', 'Savings', 'Credit Card').
- `statement_period_start`: First day of the statement period (format YYYY-MM-DD).
- `statement_period_end`: Last day of the statement period (format YYYY-MM-DD).
- `currency`: The currency of the amounts, as an ISO code if you can infer it.
- `opening_balance`: The previous/opening balance at the start of the period.
- `closing_balance`: The final balance at the end of the period.
- `transactions`: A list of every row in the transaction table, in the order printed.
  Each transaction should be an object with:
    - `date`: Date of the transaction (format YYYY-MM-DD).
    - `description`: The description text for the row.
    - `reference`: The reference or cheque number, if one is shown.
    - `withdrawal`: Amount withdrawn/debited on that row, as a positive number.
    - `deposit`: Amount deposited/credited on that row, as a positive number.
    - `balance`: The running balance shown for that row.
- `total_withdrawals`: The total withdrawals/debits reported on the statement.
- `total_deposits`: The total deposits/credits reported on the statement.

Rules:
- Report all amounts as numbers, not strings. Do not include currency symbols or
  thousands separators (e.g. 1515.63, not "$1,515.63").
- A negative balance may be printed with a minus sign or in parentheses; return it
  as a negative number.
- Each transaction row has either a withdrawal or a deposit, not both. Use null
  for the column that is blank on that row.
- Do not include the "Previous balance" or "*** Totals ***" rows in `transactions`;
  map them to `opening_balance`, `total_withdrawals`, and `total_deposits` instead.
- If a field is not found or not applicable, use null.
- Output only the JSON object itself.
"""

# Fields whose values should be coerced from strings like "$1,515.63" to numbers.
AMOUNT_FIELDS = {
    "opening_balance",
    "closing_balance",
    "total_withdrawals",
    "total_deposits",
    "withdrawal",
    "deposit",
    "balance",
}


# ============================================
# INPUT DISCOVERY
# ============================================

def find_statement(folder: Path) -> Path:
    """Find exactly one supported statement file in the given folder."""
    candidates = sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_TYPES
    )
    if not candidates:
        raise SystemExit(
            f"No PDF or image files found in {folder}. "
            "Pass the filename explicitly: python data_extraction.py statement.pdf"
        )
    if len(candidates) > 1:
        names = ", ".join(p.name for p in candidates)
        raise SystemExit(
            f"Multiple candidate files found ({names}). "
            "Pass the one you want: python data_extraction.py statement.pdf"
        )
    return candidates[0]


def resolve_input(arg: str | None) -> Path:
    if arg is None:
        return find_statement(Path.cwd())

    path = Path(arg).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.exists():
        raise SystemExit(f"File not found: {path}")
    if path.suffix.lower() not in SUPPORTED_TYPES:
        supported = ", ".join(sorted(SUPPORTED_TYPES))
        raise SystemExit(f"Unsupported file type '{path.suffix}'. Supported: {supported}")
    return path


# ============================================
# GEMINI CALL
# ============================================

def build_document_part(client: genai.Client, path: Path):
    """
    Return a contents part for the statement.

    Gemini reads PDFs natively, so there is no need to rasterize pages first.
    Small files are sent inline; large ones are uploaded via the Files API.
    """
    mime_type = SUPPORTED_TYPES[path.suffix.lower()]
    size = path.stat().st_size

    if size <= INLINE_LIMIT_BYTES:
        return types.Part.from_bytes(data=path.read_bytes(), mime_type=mime_type)

    print(f"File is {size / 1_048_576:.1f} MB; uploading via the Files API...")
    return client.files.upload(file=str(path))


def call_gemini(client: genai.Client, path: Path, model: str) -> str:
    """Send the statement plus the extraction prompt, requesting JSON back."""
    contents = [
        build_document_part(client, path),
        types.Part.from_text(text=EXTRACTION_PROMPT),
    ]

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        response_mime_type="application/json",
        max_output_tokens=32768,
    )

    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=config,
    )

    text = response.text
    if not text:
        raise SystemExit(
            "The model returned an empty response. This usually means the output token "
            "limit was hit or the request was blocked. Try a smaller page range or a "
            "higher max_output_tokens."
        )
    return text


# ============================================
# RESPONSE PARSING AND CLEANUP
# ============================================

def parse_json_response(text: str) -> dict:
    """Parse the model's JSON, tolerating markdown code fences."""
    cleaned = text.strip()

    fenced = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
    if fenced:
        cleaned = fenced.group(1).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as err:
        raise SystemExit(
            f"Could not parse the model response as JSON ({err}).\n"
            f"--- raw response ---\n{text}\n--- end raw response ---"
        )


def to_number(value):
    """Turn '$1,515.63' or '(62.47)' into a float; leave anything else alone."""
    if isinstance(value, (int, float)) or value is None:
        return value
    if not isinstance(value, str):
        return value

    raw = value.strip()
    if not raw:
        return None

    negative = raw.startswith("(") and raw.endswith(")")
    stripped = re.sub(r"[^\d.\-]", "", raw)
    if stripped in ("", "-", ".", "-."):
        return value

    try:
        number = float(stripped)
    except ValueError:
        return value
    return -abs(number) if negative else number


def clean_amounts(data: dict) -> dict:
    """Normalize amount fields to numbers, in case the model returned strings."""
    for key in list(data):
        if key in AMOUNT_FIELDS:
            data[key] = to_number(data[key])

    for txn in data.get("transactions") or []:
        if isinstance(txn, dict):
            for key in list(txn):
                if key in AMOUNT_FIELDS:
                    txn[key] = to_number(txn[key])
    return data


def reconcile(data: dict) -> list[str]:
    """Sanity-check the extracted numbers and return any warnings."""
    warnings: list[str] = []
    transactions = data.get("transactions") or []

    if not transactions:
        warnings.append("No transactions were extracted.")
        return warnings

    def summed(field: str) -> float:
        return round(
            sum(
                txn[field]
                for txn in transactions
                if isinstance(txn, dict) and isinstance(txn.get(field), (int, float))
            ),
            2,
        )

    checks = [
        ("total_withdrawals", summed("withdrawal"), "withdrawals"),
        ("total_deposits", summed("deposit"), "deposits"),
    ]
    for field, computed, label in checks:
        reported = data.get(field)
        if isinstance(reported, (int, float)) and abs(reported - computed) > 0.01:
            warnings.append(
                f"Sum of {label} ({computed}) does not match the reported "
                f"{field} ({reported})."
            )

    opening = data.get("opening_balance")
    closing = data.get("closing_balance")
    if all(isinstance(v, (int, float)) for v in (opening, closing)):
        expected = round(opening + summed("deposit") - summed("withdrawal"), 2)
        if abs(expected - closing) > 0.01:
            warnings.append(
                f"Opening balance plus net activity is {expected}, but the closing "
                f"balance reads {closing}."
            )

    return warnings


# ============================================
# MAIN
# ============================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract bank statement transactions to JSON using Gemini."
    )
    parser.add_argument(
        "statement",
        nargs="?",
        help="Path to the statement (PDF/PNG/JPG). Defaults to the only supported "
             "file in the current folder.",
    )
    parser.add_argument(
        "-o", "--out",
        default=DEFAULT_OUTPUT,
        help=f"Output JSON path (default: {DEFAULT_OUTPUT} in the working folder).",
    )
    parser.add_argument("--model", default=MODEL_NAME, help=f"Gemini model (default: {MODEL_NAME}).")
    parser.add_argument("--api-key", default=None, help="Gemini API key (overrides API_KEY and env var).")
    args = parser.parse_args()

    api_key = args.api_key or API_KEY or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        print(
            "No Gemini API key found. Set API_KEY at the top of this file, export "
            "GEMINI_API_KEY, or pass --api-key.",
            file=sys.stderr,
        )
        return 2

    statement_path = resolve_input(args.statement)
    output_path = Path(args.out).expanduser()
    if not output_path.is_absolute():
        output_path = Path.cwd() / output_path

    model = args.model
    client = genai.Client(api_key=api_key)

    print(f"Reading  {statement_path.name}")
    print(f"Model    {model}")

    raw_response = call_gemini(client, statement_path, model)
    data = clean_amounts(parse_json_response(raw_response))

    # Record what produced this file, which helps when re-running on a batch.
    data["_source_file"] = statement_path.name
    data["_model"] = model

    output_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    count = len(data.get("transactions") or [])
    print(f"Wrote    {output_path} ({count} transaction{'s' if count != 1 else ''})")

    for warning in reconcile(data):
        print(f"Warning: {warning}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())