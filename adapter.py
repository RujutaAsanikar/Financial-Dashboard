"""Parser JSON -> canonical rows. Stage 1.

The isolation layer. If the parser's output shape changes, this is the only
file that changes. See CLAUDE.md sections 2 and 3 for the contract.


TWO CONVENTIONS, AND ONLY ONE OF THEM IS OURS
---------------------------------------------

`amount` is normalized by us and is account-type independent:

    amount = (deposit or 0) - (withdrawal or 0)

Money out is negative, money in is positive, on every account type. Nothing
downstream ever needs to branch on account_type to interpret an amount.

`balance`, `opening_balance` and `closing_balance` are NOT normalized. They are
the statement's own printed figures, carried through untouched, so their
meaning is whatever the bank meant -- and that changes with the account type:

    checking / savings   balance is an ASSET     (money you have)
    credit               balance is a LIABILITY  (money you owe)

which inverts the relationship between the two quantities:

    account_type        deposit/payment   purchase/withdrawal   bridge
    ----------------------------------------------------------------------
    checking, savings   balance rises     balance falls         +amount
    credit              balance falls     balance RISES         -amount

Stated as an invariant, with sign = -1 for credit and +1 otherwise:

    balance[i] - balance[i-1]  ==  amount[i] * sign
    closing_balance - opening_balance  ==  sum(amounts) * sign

The header totals `total_deposits` / `total_withdrawals` are in AMOUNT space,
not balance space -- both are positive magnitudes and their difference
reconstructs sum(amounts). They take the same sign factor, not the opposite.

Worked from fixtures/credit.json:

    2026-08-19  Giant Eagle  amount  -84.12   balance 2897.69 -> 2981.81  (+84.12)
    2026-08-21  Payment      amount +450.00   balance 3013.21 -> 2563.21  (-450.00)
    sum(amounts) = -692.39      closing - opening = +692.39

A positive `closing_balance` on a credit account is CORRECT and must not be
"fixed" by negating it: Stage 8 consumes it directly as the amount to pay off,
and Stage 9 surfaces it as-is in accounts[].closing_balance.

Any code that compares a balance against an amount must apply the sign factor.
Today that is exactly one place -- Stage 2 reconcile(). Everything else
(transfers, recurring, the summary, by_category, spending_over_time) reads
amounts only and is unaffected.
"""

import hashlib
import logging
import re
from datetime import date, datetime

logger = logging.getLogger(__name__)

# Tried in order. The parser emits ISO; the rest are defensive.
_DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%b %d, %Y", "%d %b %Y")

# Keyword rules, not exact matches: statements print "Visa Signature" and
# "Everyday Chequing", never the bare word. Evaluated in order, first match
# wins, so the ordering is load-bearing -- see the two traps below.
_TYPE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # TRAP 1: "debit" must beat every card brand. A "Visa Debit" is a
    # chequing account that happens to run on the Visa network.
    ("checking", ("debit",)),
    ("credit", ("credit card", "creditcard", "charge card", "card account",
                "line of credit", "credit line", "heloc",
                "visa", "mastercard", "master card",
                "amex", "american express", "discover", "diners")),
    ("checking", ("chequing", "checking", "current account", "demand deposit",
                  "dda", "transaction account", "everyday", "now account")),
    ("savings", ("savings", "saving", "money market", "tfsa", "isa",
                 "certificate of deposit", "gic", "term deposit")),
)

# TRAP 2: "Credit Union Checking" is a CHECKING account -- here "credit" names
# the institution, not the product. Removed before the credit keywords run.
# Note this deliberately leaves "Acme Credit Union Visa" matching as credit,
# which is right: that one really is a card.
_CREDIT_UNION = re.compile(r"\bcredit union\b")

DEFAULT_CURRENCY = "USD"

# The account dict splits in two. Stage 3 should build its INSERT from
# PERSISTED_ACCOUNT_FIELDS rather than from account.keys(), so that adding a
# validation-only field here can never silently become a database column.
PERSISTED_ACCOUNT_FIELDS = (
    "id", "bank_name", "account_holder_name", "account_last4", "account_type",
    "currency", "opening_balance", "closing_balance", "apr", "credit_limit",
)

# Statement header figures, kept only so Stage 2's check B has inputs.
VALIDATION_ONLY_ACCOUNT_FIELDS = ("total_deposits", "total_withdrawals")


def normalize_account_type(raw: str | None) -> str:
    """Free text from the statement -> checking|savings|credit|unknown.

    Biased against claiming "credit", because the two failure modes cost very
    different amounts. Missing a credit card (-> unknown) degrades visibly:
    reconciliation reports reconciled=False and no payoff entry is produced.
    Misreading a chequing account AS credit inverts reconciliation silently
    and makes Stage 9 offer to amortize a chequing account. When in doubt,
    fall through to unknown.
    """
    if not raw:
        return "unknown"
    text = _CREDIT_UNION.sub(" ", " ".join(str(raw).split()).lower())
    for account_type, keywords in _TYPE_RULES:
        if any(re.search(rf"\b{re.escape(k)}\b", text) for k in keywords):
            return account_type
    return "unknown"


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

    The account dict, and where each key ends up:

        id                   persisted   slug, f"{bank}-{last4}" lowercased
        bank_name            persisted
        account_holder_name  persisted
        account_last4        persisted   last 4 digits only, never the rest
        account_type         persisted   checking|savings|credit|unknown
        currency             persisted   defaults to USD
        opening_balance      persisted
        closing_balance      persisted
        apr                  persisted   from the upload form, not the JSON
        credit_limit         persisted   from the upload form, not the JSON
        total_deposits       VALIDATION-ONLY
        total_withdrawals    VALIDATION-ONLY

    The last two are statement header figures that exist only to give Stage 2's
    check B its inputs. They are not persisted, not in the DuckDB schema, and
    not in models.py, so they never reach the API. They ride on this dict
    because both signatures either side of them are fixed -- adapt() returns
    (account, transactions) and reconcile() takes (account, txns) -- leaving
    the account dict as the only channel between the two.

    So: anything writing this dict to the database must name its columns.
    A splat of account.keys() into an INSERT will break on those two.

    Transaction rows are the canonical model from CLAUDE.md section 3, minus
    "id", which is Stage 3's to mint once the row is about to be stored.
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
        # Statement header totals. Carried only so Stage 2 can run check B
        # against them; they are not persisted and not part of the API.
        "total_deposits": _to_float(parser_json.get("total_deposits")),
        "total_withdrawals": _to_float(parser_json.get("total_withdrawals")),
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
