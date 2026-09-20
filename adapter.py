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
    "currency", "opening_balance", "closing_balance", "apr",
)

# Statement header figures, kept only so Stage 2's check B has inputs.
VALIDATION_ONLY_ACCOUNT_FIELDS = ("total_deposits", "total_withdrawals", "totals_source")

# Provenance of total_deposits / total_withdrawals. This distinction is not
# bookkeeping -- it is what keeps check B honest.
#
# TOTALS_STATEMENT: the bank printed them. Check B then compares two figures
#   the bank asserted independently of each other, and independently of our
#   extraction. That is the whole point of the check.
#
# TOTALS_DERIVED: the bank did not print them, so we summed our own rows.
#   deposits - withdrawals is then identically sum(amounts), which is exactly
#   what check A already compares against closing - opening. Running check B
#   on these would restate check A, always agree with it, and report three
#   passing checks where only one piece of evidence exists. Stage 2 therefore
#   skips B when it sees this value. The numbers are still worth having --
#   they are real period totals, fine to display -- they just cannot audit
#   themselves.
TOTALS_STATEMENT = "statement"
TOTALS_DERIVED = "derived"


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


# A date stranded at the front of the description: "06/01 Rent Bill".
# The vision model reads the date and the description as one text run when a
# statement does not visually separate the columns, so it returns
# date: null and keeps "06/01" inside the description. The parser's own
# to_iso_date cannot rescue it either -- every pattern it knows needs a year,
# and a bare MM/DD has none. The statement period does, which is why this
# belongs here.
_LEADING_DATE = re.compile(r"^\s*(\d{1,2})[/-](\d{1,2})(?:\s|$)")


def recover_date_from_description(description: str, period_start, period_end):
    """Pull a leading MM/DD off a description, using the period for the year.

    Only ever consulted when the date field is already null, so it can never
    override a date the parser did read. Returns None unless the recovered
    day actually falls inside the statement period -- without that check a
    reference number like "12/34 Payment" would parse as a date.
    """
    if not isinstance(description, str):
        return None
    match = _LEADING_DATE.match(description)
    if not match:
        return None

    month, day = int(match.group(1)), int(match.group(2))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None

    start = parse_date(period_start)
    end = parse_date(period_end)
    if start is None:
        return None

    # A period can straddle a year end: Dec 15 - Jan 14 means a December row
    # belongs to the start year and a January row to the next.
    for year in {start.year, (end or start).year}:
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if start <= candidate <= (end or date(start.year + 1, 12, 31)):
            return candidate
    return None


def _to_float(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def adapt(parser_json: dict, apr: float | None = None) -> tuple[dict, list[dict]]:
    """Convert one parser statement into (account, transactions).

    apr comes from the upload form, not the statement.
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
        total_deposits       VALIDATION-ONLY  printed, else derived from rows
        total_withdrawals    VALIDATION-ONLY  printed, else derived from rows
        totals_source        VALIDATION-ONLY  which of those two it was

    The last three are statement header figures that exist to give Stage 2's
    check B its inputs. They are not persisted, not in the DuckDB schema, and
    not in models.py, so they never reach the API. They ride on this dict
    because both signatures either side of them are fixed -- adapt() returns
    (account, transactions) and reconcile() takes (account, txns) -- leaving
    the account dict as the only channel between the two.

    When the statement did not print the totals we sum our own rows and set
    totals_source to TOTALS_DERIVED. The figures are then real and fine to
    display, but Stage 2 will skip check B on them, because derived totals
    reduce that check to a restatement of check A. See the constants above.

    So: anything writing this dict to the database must name its columns.
    A splat of account.keys() into an INSERT will break on those three.

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

    # An APR is a property of a borrowing account. A chequing account has
    # none, so an APR typed into the upload form against one is a mistake
    # rather than data, and is dropped here.
    #
    # The guard lives in the adapter, not the endpoint, because this is the
    # single point every path goes through -- /api/upload, /api/upload-batch,
    # and any direct call. Putting it in a route would leave the next caller
    # to remember it.
    account_type = normalize_account_type(parser_json.get("account_type"))
    if account_type != "credit" and apr is not None:
        logger.warning("Ignoring apr=%s on %s account %s: only a credit account "
                       "can carry one", apr, account_type, account_id)
        apr = None

    account = {
        "id": account_id,
        "bank_name": parser_json.get("bank_name"),
        "account_holder_name": parser_json.get("account_holder_name"),
        "account_last4": acct_last4,
        "account_type": account_type,
        "currency": parser_json.get("currency") or DEFAULT_CURRENCY,
        "opening_balance": _to_float(parser_json.get("opening_balance")),
        "closing_balance": _to_float(parser_json.get("closing_balance")),
        # Statement header totals, filled in after the loop below so that they
        # can fall back to being derived from the rows. Not persisted, not
        # part of the API.
        "total_deposits": _to_float(parser_json.get("total_deposits")),
        "total_withdrawals": _to_float(parser_json.get("total_withdrawals")),
        "totals_source": None,
        "apr": apr,
    }

    transactions: list[dict] = []
    # Gross column sums, for deriving the header totals if they weren't
    # printed. Gross rather than net because that is what a statement prints:
    # a row with both columns filled contributes to both sums, where its
    # signed `amount` would only carry the net.
    gross_deposits = 0.0
    gross_withdrawals = 0.0
    recovered_dates = 0
    dated_from_period = 0
    period_start_date = parse_date(parser_json.get("statement_period_start"))

    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            logger.warning("Skipping row %d: expected an object, got %s",
                           index, type(row).__name__)
            continue

        txn_date = parse_date(row.get("date"))
        if txn_date is None:
            txn_date = recover_date_from_description(
                row.get("description"),
                parser_json.get("statement_period_start"),
                parser_json.get("statement_period_end"),
            )
            if txn_date is not None:
                recovered_dates += 1
        if txn_date is None and period_start_date is not None:
            # TEMPORARY, pending a parser fix. Keeping the row with a
            # fabricated date beats dropping it, but the date IS fabricated:
            # every such row lands on the same day, so spending_over_time
            # collapses into one bucket and anything date-ordered is fiction.
            # Recurring detection is unaffected only by luck -- identical
            # dates give zero-length gaps, which match_cadence rejects.
            txn_date = period_start_date
            dated_from_period += 1
        if txn_date is None:
            logger.warning("Skipping row %d: unparseable date %r", index, row.get("date"))
            continue

        withdrawal = _to_float(row.get("withdrawal")) or 0.0
        deposit = _to_float(row.get("deposit")) or 0.0
        amount = round(deposit - withdrawal, 2)

        # Preserved verbatim; only a copy is inspected for the skip test.
        description = row.get("description")
        description = description if isinstance(description, str) else ""

        # A row that moves no money contributes nothing to any sum, category
        # or average -- it only pads transaction_count and the transaction
        # list. On OCR output it is usually not a transaction at all: section
        # headings like "Banking/Debit Card Withdrawals and Purchases" get
        # picked up as rows with no amount attached.
        #
        # CLAUDE.md skips these only when the description is ALSO empty. That
        # is too narrow for real extractor output, where the junk rows carry
        # plenty of text. If a bank ever prints a genuine $0.00 line (a waived
        # fee, a declined authorization), dropping it loses no money -- it
        # contributes zero to every figure by definition.
        if amount == 0:
            logger.warning("Skipping row %d: zero amount (%r)",
                           index, description[:60] or "<no description>")
            continue

        gross_deposits += deposit
        gross_withdrawals += withdrawal

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

    # Header totals: keep what the statement printed, otherwise derive from
    # the rows we kept. `totals_source` records which, because the difference
    # decides whether Stage 2's check B may use them at all -- derived totals
    # make that check circular. See TOTALS_DERIVED in this module's docstring.
    if account["total_deposits"] is None or account["total_withdrawals"] is None:
        account["total_deposits"] = round(gross_deposits, 2)
        account["total_withdrawals"] = round(gross_withdrawals, 2)
        account["totals_source"] = TOTALS_DERIVED
        logger.info("Derived header totals for %s: deposits %.2f, withdrawals %.2f "
                    "(not printed on the statement; check B will be skipped)",
                    account_id, account["total_deposits"], account["total_withdrawals"])
    else:
        account["totals_source"] = TOTALS_STATEMENT

    if dated_from_period:
        logger.warning(
            "TEMPORARY FALLBACK: %d row(s) on %s had no date anywhere and were "
            "stamped with the statement start date %s. Those rows are in the "
            "right statement but the WRONG DAY -- spending_over_time and any "
            "date ordering are unreliable for them. Remove this once the "
            "parser populates date.",
            dated_from_period, account_id, period_start_date)

    if recovered_dates:
        logger.warning(
            "Recovered %d date(s) from the description on %s; the parser left "
            "date null and kept the date inside the description text",
            recovered_dates, account_id)

    logger.info("Adapted %d/%d rows for account %s", len(transactions), len(rows), account_id)
    return account, transactions
