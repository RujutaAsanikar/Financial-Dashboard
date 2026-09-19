"""Parser JSON -> canonical rows. Stage 1.

The isolation layer. If the parser's output shape changes, this is the only
file that changes. See CLAUDE.md sections 2 and 3 for the contract.
"""

import hashlib
import logging
import re
from datetime import date, datetime

logger = logging.getLogger(__name__)

# Tried in order. The parser emits ISO; the rest are defensive.
_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%b %d, %Y", "%d %b %Y")

_ACCOUNT_TYPES = {
    "chequing": "checking",
    "chequing account": "checking",
    "checking": "checking",
    "checking account": "checking",
    "savings": "savings",
    "saving": "savings",
    "savings account": "savings",
    "credit card": "credit",
    "credit": "credit",
    "visa": "credit",
    "mastercard": "credit",
    "amex": "credit",
}

DEFAULT_CURRENCY = "USD"


def normalize_account_type(raw: str | None) -> str:
    """Free text from the statement -> checking|savings|credit|unknown."""
    if not raw:
        return "unknown"
    return _ACCOUNT_TYPES.get(" ".join(raw.split()).lower(), "unknown")


def last4(account_number: str | None) -> str | None:
    """Last 4 digits only. The full number must never leave this function."""
    if not account_number:
        return None
    digits = re.sub(r"\D", "", account_number)
    return digits[-4:] if len(digits) >= 4 else (digits or None)


def _slug(text: str) -> str:
    """Lowercase, non-alphanumerics collapsed to a single '-'."""
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")


def build_account_id(bank_name: str | None, acct_last4: str | None,
                     period_start: str | None) -> str:
    """Deterministic account slug. Falls back to a period hash if both parts are null."""
    parts = [p for p in (bank_name, acct_last4) if p]
    if parts:
        slug = _slug("-".join(str(p) for p in parts))
        if slug:
            return slug
    digest = hashlib.sha256((period_start or "").encode()).hexdigest()[:8]
    return f"unknown-{digest}"


def parse_date(raw) -> date | None:
    """Parse to datetime.date, or None if unparseable."""
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    if isinstance(raw, datetime):
        return raw.date()
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _to_float(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def adapt(parser_json: dict, apr: float | None = None,
          credit_limit: float | None = None) -> tuple[dict, list[dict]]:
    """Convert one parser statement into (account, transactions).

    apr and credit_limit come from the upload form, not the statement.
    Raises ValueError if the payload has no "transactions" key.
    """
    if not isinstance(parser_json, dict) or "transactions" not in parser_json:
        raise ValueError(
            "parser_json is missing the required 'transactions' key; "
            f"got keys: {sorted(parser_json) if isinstance(parser_json, dict) else type(parser_json).__name__}"
        )

    rows = parser_json.get("transactions") or []
    if not isinstance(rows, list):
        raise ValueError(f"'transactions' must be a list, got {type(rows).__name__}")

    acct_last4 = last4(parser_json.get("account_number"))
    account_id = build_account_id(
        parser_json.get("bank_name"), acct_last4, parser_json.get("statement_period_start")
    )

    account = {
        "id": account_id,
        "bank_name": parser_json.get("bank_name"),
        "account_holder_name": parser_json.get("account_holder_name"),
        "account_last4": acct_last4,
        "account_type": normalize_account_type(parser_json.get("account_type")),
        "currency": parser_json.get("currency") or DEFAULT_CURRENCY,
        "opening_balance": _to_float(parser_json.get("opening_balance")),
        "closing_balance": _to_float(parser_json.get("closing_balance")),
        "apr": apr,
        "credit_limit": credit_limit,
    }

    transactions: list[dict] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            logger.warning("Skipping row %d: expected an object, got %s",
                           index, type(row).__name__)
            continue

        txn_date = parse_date(row.get("date"))
        if txn_date is None:
            logger.warning("Skipping row %d: unparseable date %r", index, row.get("date"))
            continue

        withdrawal = _to_float(row.get("withdrawal")) or 0.0
        deposit = _to_float(row.get("deposit")) or 0.0
        amount = round(deposit - withdrawal, 2)

        # Preserved verbatim; only a copy is inspected for the skip test.
        description = row.get("description")
        description = description if isinstance(description, str) else ""

        if amount == 0 and not description.strip():
            logger.warning("Skipping row %d: no amount and no description", index)
            continue

        transactions.append({
            "account_id": account_id,
            "date": txn_date,
            "description": description,
            "amount": amount,
            "balance": _to_float(row.get("balance")),
            "reference": row.get("reference"),
            # Populated by later stages; present so the row shape is stable.
            "merchant": None,
            "category": None,
            "confidence": None,
            "is_transfer": False,
            "is_recurring": False,
        })

    logger.info("Adapted %d/%d rows for account %s", len(transactions), len(rows), account_id)
    return account, transactions
