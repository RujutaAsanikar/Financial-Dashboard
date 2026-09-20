"""python
#!/usr/bin/env python3

Extract structured transaction data from bank statements using Anthropic's
Claude API, one or many files at a time.

Usage:
    python data_extraction.py statement.pdf
    python data_extraction.py jan.pdf feb.pdf mar.png
    python data_extraction.py "statements/*.pdf" --combined all.json
    python data_extraction.py --all

Writes <name>_transaction_data.json per input, or transaction_data.json for a
single file.

Requires:
    pip install anthropic

The API key is read from:
    1. --api-key
    2. ANTHROPIC_API_KEY environment variable
    3. CLAUDE_API environment variable
    4. CLAUDE_API_KEY environment variable
    5. .env file in the working folder

The output shape is guaranteed by the API when schema mode is enabled.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import random
import re
import sys
import time
from pathlib import Path

import anthropic


# ============================================
# CONFIGURATION
# ============================================

# API key is intentionally NOT stored in this file.
# Put it in .env:
#
# ANTHROPIC_API_KEY=your_key_here

MODEL_NAME = os.environ.get(
    "ANTHROPIC_MODEL",
    "claude-opus-4-8",
)

FALLBACK_MODELS = [
    "claude-sonnet-5",
    "claude-haiku-4-5-20251001",
]

MAX_TOKENS = 16384

DEFAULT_OUTPUT = "transaction_data.json"

MAX_FILE_BYTES = 20 * 1024 * 1024

SUPPORTED_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

MAX_ATTEMPTS = 5
INITIAL_BACKOFF_SECONDS = 2.0
MAX_BACKOFF_SECONDS = 32.0

RETRYABLE_STATUS = {
    408,
    409,
    429,
    500,
    502,
    503,
    504,
    529,
}


# ============================================
# OUTPUT SCHEMA
# ============================================

NULLABLE_STRING = {
    "type": ["string", "null"]
}

NULLABLE_NUMBER = {
    "type": ["number", "null"]
}


TRANSACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "date": {
            "type": "string",
            "description": (
                "Transaction date as YYYY-MM-DD. If the row prints no date "
                "of its own, use the most recent date printed above it."
            ),
        },
        "description": {
            "type": "string",
            "description": "The description text for the row, as printed.",
        },
        "reference": {
            **NULLABLE_STRING,
            "description": (
                "Reference or cheque number as a string, preserving leading "
                "zeros. Null when the row shows none."
            ),
        },
        "withdrawal": {
            **NULLABLE_NUMBER,
            "description": (
                "Amount withdrawn or debited, as a positive number. "
                "Null when that column is blank on this row."
            ),
        },
        "deposit": {
            **NULLABLE_NUMBER,
            "description": (
                "Amount deposited or credited, as a positive number. "
                "Null when that column is blank on this row."
            ),
        },
        "balance": {
            **NULLABLE_NUMBER,
            "description": (
                "Running balance printed for this row. Negative if shown "
                "with a minus sign or in parentheses."
            ),
        },
    },
    "required": [
        "date",
        "description",
        "reference",
        "withdrawal",
        "deposit",
        "balance",
    ],
    "additionalProperties": False,
}


STATEMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "bank_name": {
            **NULLABLE_STRING,
            "description": "Name of the bank or financial institution.",
        },
        "account_holder_name": {
            **NULLABLE_STRING,
            "description": "Name of the account holder.",
        },
        "account_number": {
            **NULLABLE_STRING,
            "description": (
                "Account number exactly as shown, keeping dashes and masking. "
                "If printed across two lines, join it."
            ),
        },
        "account_type": {
            **NULLABLE_STRING,
            "description": (
                "E.g. Chequing, Savings, Credit Card."
            ),
        },
        "statement_period_start": {
            **NULLABLE_STRING,
            "description": "First day of the period, YYYY-MM-DD.",
        },
        "statement_period_end": {
            **NULLABLE_STRING,
            "description": "Last day of the period, YYYY-MM-DD.",
        },
        "currency": {
            **NULLABLE_STRING,
            "description": (
                "ISO currency code if printed or unambiguous from the "
                "document. Null if you would be guessing."
            ),
        },
        "opening_balance": {
            **NULLABLE_NUMBER,
            "description": (
                "Previous or opening balance at the start of the period."
            ),
        },
        "closing_balance": {
            **NULLABLE_NUMBER,
            "description": (
                "Final balance at the end of the period."
            ),
        },
        "transactions": {
            "type": "array",
            "description": (
                "Every row of the transaction table in printed order, "
                "excluding the opening-balance and totals rows."
            ),
            "items": TRANSACTION_SCHEMA,
        },
        "total_withdrawals": {
            **NULLABLE_NUMBER,
            "description": "Total withdrawals or debits as reported.",
        },
        "total_deposits": {
            **NULLABLE_NUMBER,
            "description": "Total deposits or credits as reported.",
        },
    },
    "required": [
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
    ],
    "additionalProperties": False,
}


SYSTEM_INSTRUCTION = (
    "You are an expert at extracting structured information from banking "
    "documents: chequing and savings account statements, and credit card "
    "statements. Read every row of the transaction table carefully, preserve "
    "the order in which rows appear, and never invent a value you cannot see "
    "in the document."
)


EXTRACTION_PROMPT = """Extract the full contents of this bank statement.

The document may span several pages. Treat it as one statement and return one combined object.

Rules:
- Include every row of the transaction table, in the order printed.
- Report amounts as plain numbers: 1515.63, not "$1,515.63".
- A balance shown with a minus sign or in parentheses is negative.
- Each row has either a withdrawal or a deposit, not both. Use null for the blank column.
- Do not put the "Previous balance" or "*** Totals ***" rows in the transactions list.
  Map them to opening_balance, total_withdrawals and total_deposits instead.
- Use null for anything the statement does not show. Do not guess.
"""


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

def expand_inputs(
    patterns: list[str],
    use_all: bool,
) -> list[Path]:

    if use_all or not patterns:
        found = sorted(
            p
            for p in Path.cwd().iterdir()
            if p.is_file()
            and p.suffix.lower() in SUPPORTED_TYPES
        )

        if not found:
            raise SystemExit(
                "No PDF or image files found here. "
                "Pass filenames explicitly."
            )

        if not use_all and len(found) > 1:
            raise SystemExit(
                f"Multiple candidates "
                f"({', '.join(p.name for p in found)}).\n"
                "Name the files you want, or pass --all "
                "to process every one."
            )

        return found

    resolved: list[Path] = []
    seen: set[Path] = set()

    for pattern in patterns:

        expanded = (
            [
                Path(m)
                for m in sorted(Path.cwd().glob(pattern))
            ]
            if any(ch in pattern for ch in "*?[")
            else []
        )

        if not expanded:
            candidate = Path(pattern).expanduser()

            if not candidate.is_absolute():
                candidate = Path.cwd() / candidate

            if candidate.is_dir():
                expanded = sorted(
                    p
                    for p in candidate.iterdir()
                    if p.is_file()
                    and p.suffix.lower() in SUPPORTED_TYPES
                )

                if not expanded:
                    print(
                        f"Skipping {pattern}: "
                        "directory has no supported files",
                        file=sys.stderr,
                    )
                    continue
            else:
                expanded = [candidate]

        for path in expanded:

            if not path.exists():
                print(
                    f"Skipping {path.name}: file not found",
                    file=sys.stderr,
                )
                continue

            if path.suffix.lower() not in SUPPORTED_TYPES:
                print(
                    f"Skipping {path.name}: "
                    f"unsupported type '{path.suffix}'",
                    file=sys.stderr,
                )
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

    if not batch and args.out:
        target = Path(args.out).expanduser()
    else:
        target = Path(
            f"{path.stem}_transaction_data.json"
        )

    if args.out_dir:
        directory = Path(args.out_dir).expanduser()

        if not directory.is_absolute():
            directory = Path.cwd() / directory

        directory.mkdir(
            parents=True,
            exist_ok=True,
        )

        return directory / target.name

    if not target.is_absolute():
        target = Path.cwd() / target

    return target


# ============================================
# ENVIRONMENT VARIABLES
# ============================================

def load_local_env(filename: str = ".env") -> None:
    """
    Load KEY=value lines from a local .env file.

    Existing environment variables win, so values already defined
    in the shell or CI environment are not overwritten.
    """

    path = Path.cwd() / filename

    if not path.is_file():
        return

    try:
        lines = path.read_text(
            encoding="utf-8"
        ).splitlines()
    except OSError:
        return

    for line in lines:

        line = line.strip()

        if (
            not line
            or line.startswith("#")
            or "=" not in line
        ):
            continue

        key, _, value = line.partition("=")

        key = key.strip()
        value = value.strip()

        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in "\"'"
        ):
            value = value[1:-1]

        if key:
            os.environ.setdefault(
                key,
                value,
            )


def resolve_api_key(
    explicit: str | None,
) -> str | None:

    """
    Find the API key.

    Order:
        1. --api-key
        2. ANTHROPIC_API_KEY
        3. CLAUDE_API
        4. CLAUDE_API_KEY
    """

    if explicit:
        return explicit

    for name in (
        "ANTHROPIC_API_KEY",
        "CLAUDE_API",
        "CLAUDE_API_KEY",
    ):
        value = os.environ.get(name)

        if value:
            return value

    return None


# ============================================
# CLAUDE CALL
# ============================================

def build_document_block(path: Path) -> dict:

    media_type = SUPPORTED_TYPES[
        path.suffix.lower()
    ]

    size = path.stat().st_size

    if size > MAX_FILE_BYTES:
        raise SystemExit(
            f"{path.name} is "
            f"{size / 1_048_576:.1f} MB, over the "
            f"{MAX_FILE_BYTES / 1_048_576:.0f} MB "
            "inline limit. Split the PDF and run it in parts."
        )

    encoded = base64.standard_b64encode(
        path.read_bytes()
    ).decode("ascii")

    block_type = (
        "document"
        if media_type == "application/pdf"
        else "image"
    )

    return {
        "type": block_type,
        "source": {
            "type": "base64",
            "media_type": media_type,
            "data": encoded,
        },
    }


def call_claude_once(
    client,
    path: Path,
    model: str,
    max_tokens: int,
    use_schema: bool,
):

    request = {
        "model": model,
        "max_tokens": max_tokens,
        "system": SYSTEM_INSTRUCTION,
        "messages": [
            {
                "role": "user",
                "content": [
                    build_document_block(path),
                    {
                        "type": "text",
                        "text": EXTRACTION_PROMPT,
                    },
                ],
            }
        ],
    }

    if use_schema:
        request["output_config"] = {
            "format": {
                "type": "json_schema",
                "schema": STATEMENT_SCHEMA,
            }
        }

    response = client.messages.create(
        **request
    )

    if response.stop_reason == "max_tokens":
        raise SystemExit(
            f"The response hit the {max_tokens}-token "
            "limit and was cut off, so the JSON is incomplete. "
            "Re-run with a higher --max-tokens, or split the statement."
        )

    if response.stop_reason == "refusal":
        raise SystemExit(
            "Claude declined to process this document. "
            "If it is a genuine bank statement, check that "
            "the file is not corrupted or mislabelled."
        )

    text = "".join(
        block.text
        for block in response.content
        if block.type == "text"
    )

    if not text.strip():
        raise SystemExit(
            "The model returned an empty response."
        )

    return (
        parse_json_response(text),
        response.usage,
    )


def call_claude(
    client,
    path: Path,
    model: str,
    max_tokens: int,
    use_schema: bool,
    fallbacks: list[str] | None = None,
    max_attempts: int = MAX_ATTEMPTS,
):

    models_to_try = [
        model
    ] + [
        m
        for m in (fallbacks or [])
        if m != model
    ]

    last_exc: Exception | None = None
    schema_enabled = use_schema

    for model_index, current_model in enumerate(
        models_to_try
    ):

        if model_index > 0:
            print(
                f"Falling back to {current_model}"
            )

        backoff = INITIAL_BACKOFF_SECONDS

        for attempt in range(
            1,
            max_attempts + 1,
        ):

            try:

                data, usage = call_claude_once(
                    client,
                    path,
                    current_model,
                    max_tokens,
                    schema_enabled,
                )

                return (
                    data,
                    current_model,
                    usage,
                )

            except SystemExit:
                raise

            except Exception as exc:

                last_exc = exc

                if (
                    schema_enabled
                    and is_schema_error(exc)
                ):
                    print(
                        "Schema rejected by the API; "
                        "retrying with prompt-only JSON.",
                        file=sys.stderr,
                    )

                    schema_enabled = False
                    continue

                if not is_retryable(exc):
                    raise

                if attempt == max_attempts:

                    plural = (
                        "s"
                        if max_attempts != 1
                        else ""
                    )

                    print(
                        f"{current_model} still failing "
                        f"after {max_attempts} "
                        f"attempt{plural}.",
                        file=sys.stderr,
                    )

                    break

                wait = min(
                    backoff,
                    MAX_BACKOFF_SECONDS,
                ) * (
                    1 + random.random() * 0.25
                )

                print(
                    f"  transient error "
                    f"({describe_status(exc)}); "
                    f"retry {attempt}/"
                    f"{max_attempts - 1} "
                    f"in {wait:.1f}s",
                    file=sys.stderr,
                )

                time.sleep(wait)

                backoff *= 2

        if (
            last_exc is not None
            and is_connection_error(last_exc)
        ):
            raise last_exc

    raise (
        last_exc
        if last_exc
        else RuntimeError(
            "No models were attempted."
        )
    )


def status_of(
    exc: Exception,
) -> int | None:

    return getattr(
        exc,
        "status_code",
        None,
    )


def describe_status(
    exc: Exception,
) -> str:

    status = status_of(exc)

    if status:
        return (
            f"{type(exc).__name__} {status}"
        )

    return type(exc).__name__


def is_connection_error(
    exc: Exception,
) -> bool:

    if status_of(exc) is not None:
        return False

    connection_error = getattr(
        anthropic,
        "APIConnectionError",
        None,
    )

    if (
        connection_error is not None
        and isinstance(exc, connection_error)
    ):
        return True

    detail = (
        f"{type(exc).__name__}: {exc}"
    ).lower()

    return any(
        marker in detail
        for marker in (
            "getaddrinfo",
            "connection",
            "timed out",
            "timeout",
            "ssl",
        )
    )


def is_retryable(
    exc: Exception,
) -> bool:

    if status_of(exc) in RETRYABLE_STATUS:
        return True

    connection_error = getattr(
        anthropic,
        "APIConnectionError",
        None,
    )

    if (
        connection_error is not None
        and isinstance(exc, connection_error)
    ):
        return True

    detail = (
        f"{type(exc).__name__}: {exc}"
    ).lower()

    return any(
        marker in detail
        for marker in (
            "overloaded",
            "timeout",
            "timed out",
            "connection",
        )
    )


def is_schema_error(
    exc: Exception,
) -> bool:

    if status_of(exc) != 400:
        return False

    detail = str(exc).lower()

    return any(
        marker in detail
        for marker in (
            "schema",
            "output_config",
            "output_format",
            "grammar",
        )
    )


# ============================================
# RESPONSE PARSING
# ============================================

def parse_json_response(
    text: str,
) -> dict:

    cleaned = text.strip()

    fenced = re.search(
        r"```(?:json)?\s*(.*?)```",
        cleaned,
        re.DOTALL,
    )

    if fenced:
        cleaned = fenced.group(1).strip()

    try:
        return json.loads(cleaned)

    except json.JSONDecodeError as err:

        raise SystemExit(
            "Could not parse the response as JSON "
            f"({err}).\n"
            "--- raw response ---\n"
            f"{text[:4000]}\n"
            "--- end raw response ---"
        )


def to_number(value):

    if (
        isinstance(value, (int, float))
        or value is None
    ):
        return value

    if not isinstance(value, str):
        return value

    raw = value.strip()

    if not raw:
        return None

    negative = (
        (
            raw.startswith("(")
            and raw.endswith(")")
        )
        or raw.startswith("-")
    )

    stripped = re.sub(
        r"[^\d.]",
        "",
        raw,
    )

    if stripped in ("", "."):
        return value

    try:
        number = float(stripped)
    except ValueError:
        return value

    return (
        -number
        if negative
        else number
    )


def clean_amounts(
    data: dict,
) -> dict:

    for key in list(data):

        if key in AMOUNT_FIELDS:
            data[key] = to_number(
                data[key]
            )

    for txn in (
        data.get("transactions") or []
    ):

        if isinstance(txn, dict):

            for key in list(txn):

                if key in AMOUNT_FIELDS:
                    txn[key] = to_number(
                        txn[key]
                    )

    return data


# ============================================
# RECONCILIATION
# ============================================

def reconcile(
    data: dict,
) -> list[str]:

    warnings: list[str] = []

    transactions = (
        data.get("transactions") or []
    )

    if not transactions:
        warnings.append(
            "No transactions were extracted."
        )
        return warnings

    def summed(
        field: str,
    ) -> float:

        return round(
            sum(
                txn[field]
                for txn in transactions
                if (
                    isinstance(txn, dict)
                    and isinstance(
                        txn.get(field),
                        (int, float),
                    )
                )
            ),
            2,
        )

    for (
        field,
        computed,
        label,
    ) in (
        (
            "total_withdrawals",
            summed("withdrawal"),
            "withdrawals",
        ),
        (
            "total_deposits",
            summed("deposit"),
            "deposits",
        ),
    ):

        reported = data.get(field)

        if (
            isinstance(
                reported,
                (int, float),
            )
            and abs(
                reported - computed
            ) > 0.01
        ):

            warnings.append(
                f"Sum of {label} ({computed}) "
                f"does not match the reported "
                f"{field} ({reported})."
            )

    previous = data.get(
        "opening_balance"
    )

    for index, txn in enumerate(
        transactions,
        start=1,
    ):

        if not isinstance(txn, dict):
            continue

        balance = txn.get(
            "balance"
        )

        if not isinstance(
            previous,
            (int, float),
        ) or not isinstance(
            balance,
            (int, float),
        ):

            if isinstance(
                balance,
                (int, float),
            ):
                previous = balance

            continue

        change = (
            txn.get("deposit") or 0
        ) - (
            txn.get("withdrawal") or 0
        )

        expected = round(
            previous + change,
            2,
        )

        if abs(
            expected - balance
        ) > 0.01:

            warnings.append(
                f"Row {index} "
                f"({txn.get('description') or '?'}): "
                f"balance reads {balance}, "
                f"but {previous} "
                f"{'+' if change >= 0 else '-'} "
                f"{abs(change)} = {expected}."
            )

        previous = balance

    closing = data.get(
        "closing_balance"
    )

    if (
        isinstance(
            previous,
            (int, float),
        )
        and isinstance(
            closing,
            (int, float),
        )
        and abs(
            previous - closing
        ) > 0.01
    ):

        warnings.append(
            f"Final row balance "
            f"({previous}) does not match "
            f"the stated closing balance "
            f"({closing})."
        )

    return warnings


# ============================================
# ERROR REPORTING
# ============================================

def describe_request_failure(
    exc: Exception,
) -> str:

    detail = (
        f"{type(exc).__name__}: {exc}"
    )

    lowered = detail.lower()

    status = status_of(exc)

    dns_markers = (
        "getaddrinfo",
        "name or service not known",
        "11001",
        "nodename nor servname",
    )

    if any(
        marker in lowered
        for marker in dns_markers
    ):

        return (
            "Network error: could not resolve "
            "api.anthropic.com.\n"
            f"  {detail}\n"
            "The request never left your machine, "
            "so this is DNS or a proxy, not your key.\n"
            "Try, in order:\n"
            "  1. nslookup api.anthropic.com\n"
            "  2. If that fails: set DNS to "
            "8.8.8.8 / 1.1.1.1\n"
            "  3. Reconnect or disconnect any VPN, "
            "or try a phone hotspot\n"
            "  4. Behind a proxy? Set "
            "HTTPS_PROXY=http://host:port"
        )

    if (
        status in (401, 403)
        or "authentication" in lowered
        or "api key" in lowered
    ):

        return (
            "Authentication error: the API key "
            "was rejected.\n"
            f"  {detail}\n"
            "Check the key in the Anthropic Console."
        )

    if (
        status == 404
        or "not_found" in lowered
    ):

        return (
            "Model error: that model was not "
            "found for this key.\n"
            f"  {detail}\n"
            "Pass a different one with --model."
        )

    if (
        status == 529
        or "overloaded" in lowered
    ):

        return (
            "The API is overloaded.\n"
            f"  {detail}\n"
            "Retries and model fallback were already "
            "attempted. Wait a few minutes and re-run."
        )

    if status == 429:

        return (
            "Rate limit reached.\n"
            f"  {detail}\n"
            "Wait and re-run, or check your rate limits."
        )

    if (
        status == 413
        or "too large" in lowered
        or "request_too_large" in lowered
    ):

        return (
            "The request was too large.\n"
            f"  {detail}\n"
            "A PDF over about 20 MB or 100 pages "
            "may need to be split."
        )

    if status == 400:

        return (
            "The API rejected the request.\n"
            f"  {detail}\n"
            "If this mentions the schema, re-run "
            "with --no-schema."
        )

    return (
        "Request to the Claude API failed.\n"
        f"  {detail}"
    )


# ============================================
# EXTRACTION
# ============================================

def extract_statement(
    client,
    path: Path,
    args,
) -> tuple[dict, list[str]]:

    (
        data,
        answering_model,
        usage,
    ) = call_claude(
        client,
        path,
        args.model,
        max_tokens=args.max_tokens,
        use_schema=not args.no_schema,
        fallbacks=(
            []
            if args.no_fallback
            else FALLBACK_MODELS
        ),
        max_attempts=max(
            1,
            args.retries,
        ),
    )

    data = clean_amounts(data)

    data["_source_file"] = path.name
    data["_model"] = answering_model

    if usage is not None:

        data["_tokens"] = {
            "input": getattr(
                usage,
                "input_tokens",
                None,
            ),
            "output": getattr(
                usage,
                "output_tokens",
                None,
            ),
        }

    return (
        data,
        reconcile(data),
    )


# ============================================
# MAIN
# ============================================

def main() -> int:

    parser = argparse.ArgumentParser(
        description=(
            "Extract bank statement transactions "
            "to JSON using the Claude API."
        ),
        epilog=(
            "Examples:\n"
            "  python data_extraction.py statement.pdf\n"
            "  python data_extraction.py jan.pdf feb.pdf\n"
            '  python data_extraction.py "statements/*.pdf" '
            "--combined all.json\n"
            "  python data_extraction.py --all"
        ),
        formatter_class=(
            argparse.RawDescriptionHelpFormatter
        ),
    )

    parser.add_argument(
        "statements",
        nargs="*",
        help="One or more files, globs, or a folder.",
    )

    parser.add_argument(
        "-o",
        "--out",
        default=None,
        help=(
            f"Output JSON for a single input "
            f"(default: {DEFAULT_OUTPUT})."
        ),
    )

    parser.add_argument(
        "--out-dir",
        default=None,
        help="Directory for the output files.",
    )

    parser.add_argument(
        "--combined",
        default=None,
        metavar="FILE",
        help=(
            "Also write every statement into one JSON array."
        ),
    )

    parser.add_argument(
        "--only-combined",
        action="store_true",
        help=(
            "Write just the combined file, "
            "not one JSON per statement."
        ),
    )

    parser.add_argument(
        "--all",
        action="store_true",
        help=(
            "Process every supported file "
            "in the current folder."
        ),
    )

    parser.add_argument(
        "--model",
        default=MODEL_NAME,
        help=(
            f"Claude model "
            f"(default: {MODEL_NAME})."
        ),
    )

    parser.add_argument(
        "--api-key",
        default=None,
        help=(
            "API key. Overrides environment variables."
        ),
    )

    parser.add_argument(
        "--max-tokens",
        type=int,
        default=MAX_TOKENS,
        help=(
            f"Output token budget "
            f"(default: {MAX_TOKENS})."
        ),
    )

    parser.add_argument(
        "--retries",
        type=int,
        default=MAX_ATTEMPTS,
        help=(
            f"Attempts per model before fallback "
            f"(default: {MAX_ATTEMPTS})."
        ),
    )

    parser.add_argument(
        "--no-fallback",
        action="store_true",
        help=(
            "Fail instead of trying another model "
            "when the primary is overloaded."
        ),
    )

    parser.add_argument(
        "--no-schema",
        action="store_true",
        help=(
            "Ask for JSON in the prompt instead "
            "of enforcing the schema."
        ),
    )

    args = parser.parse_args()

    if (
        args.only_combined
        and not args.combined
    ):
        raise SystemExit(
            "--only-combined requires --combined FILE"
        )

    # Load .env before resolving the API key.
    load_local_env()

    api_key = resolve_api_key(
        args.api_key
    )

    if not api_key:

        print(
            "No API key found.\n"
            "Set ANTHROPIC_API_KEY in your .env file "
            "or environment.",
            file=sys.stderr,
        )

        return 2

    inputs = expand_inputs(
        args.statements,
        args.all,
    )

    batch = len(inputs) > 1

    if (
        not batch
        and args.out is None
    ):
        args.out = DEFAULT_OUTPUT

    # Retries are handled by this script.
    client = anthropic.Anthropic(
        api_key=api_key,
        max_retries=0,
    )

    print(
        f"Found    {len(inputs)} "
        f"file{'s' if batch else ''}"
    )

    print(
        f"Model    {args.model}"
    )

    results: list[dict] = []
    summary: list[tuple[str, str]] = []
    failures = 0

    for position, path in enumerate(
        inputs,
        start=1,
    ):

        prefix = (
            f"[{position}/{len(inputs)}] "
            if batch
            else ""
        )

        print(
            f"\n{prefix}Reading  {path.name}"
        )

        try:

            data, warnings = extract_statement(
                client,
                path,
                args,
            )

        except SystemExit as exc:

            if not batch:
                raise

            failures += 1

            print(
                f"Failed   {path.name}: {exc}",
                file=sys.stderr,
            )

            summary.append(
                (path.name, "failed")
            )

            continue

        except Exception as exc:

            failures += 1

            print(
                f"Failed   {path.name}:\n"
                f"{describe_request_failure(exc)}",
                file=sys.stderr,
            )

            summary.append(
                (path.name, "failed")
            )

            continue

        results.append(data)

        count = len(
            data.get("transactions") or []
        )

        if not args.only_combined:

            target = output_path_for(
                path,
                args,
                batch,
            )

            target.write_text(
                json.dumps(
                    data,
                    indent=2,
                ),
                encoding="utf-8",
            )

            print(
                f"Wrote    {target} "
                f"({count} transaction"
                f"{'s' if count != 1 else ''})"
            )

        else:

            print(
                f"Parsed   {count} transaction"
                f"{'s' if count != 1 else ''}"
            )

        for warning in warnings:

            print(
                f"Warning: {path.name}: {warning}",
                file=sys.stderr,
            )

        if not warnings:

            print(
                "Checks   balances reconcile cleanly"
            )

        summary.append(
            (
                path.name,
                f"{count} txns"
                + (
                    f", {len(warnings)} warning(s)"
                    if warnings
                    else ", clean"
                ),
            )
        )

    if args.combined and results:

        combined_path = Path(
            args.combined
        ).expanduser()

        if (
            args.out_dir
            and not combined_path.is_absolute()
            and combined_path.parent == Path(".")
        ):

            combined_path = (
                Path(args.out_dir).expanduser()
                / combined_path
            )

        if not combined_path.is_absolute():

            combined_path = (
                Path.cwd()
                / combined_path
            )

        combined_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        combined_path.write_text(
            json.dumps(
                results,
                indent=2,
            ),
            encoding="utf-8",
        )

        total = sum(
            len(
                r.get("transactions") or []
            )
            for r in results
        )

        print(
            f"\nWrote    {combined_path} "
            f"({len(results)} statements, "
            f"{total} transactions)"
        )

    if batch:

        print("\nSummary")

        if summary:

            width = max(
                len(name)
                for name, _ in summary
            )

            for name, status in summary:

                print(
                    f"  {name.ljust(width)}  "
                    f"{status}"
                )

        tokens_in = sum(
            (
                r.get("_tokens") or {}
            ).get("input") or 0
            for r in results
        )

        tokens_out = sum(
            (
                r.get("_tokens") or {}
            ).get("output") or 0
            for r in results
        )

        if tokens_in or tokens_out:

            print(
                f"  {'tokens'.ljust(width)}  "
                f"{tokens_in:,} in / "
                f"{tokens_out:,} out"
            )

        if failures:

            print(
                f"  {failures} file(s) failed",
                file=sys.stderr,
            )

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

