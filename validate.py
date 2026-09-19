"""Reconciliation: verify the extraction against the statement's own arithmetic.

Stage 2. This is the answer to "how do you know the AI didn't hallucinate these
numbers?" -- we don't trust the extraction, we check it against three
independent arithmetic identities the statement already asserts about itself.

    A  sum_vs_balance     the transactions sum to the change in balance
    B  totals_vs_balance  the printed header totals agree with that change too
    C  running_balance    each row's printed balance moves by exactly its amount

A and B are whole-statement checks and answer "did we lose or invent a row?".
C is per-row and answers "which row did we get wrong?" -- it is the only check
that can point at a culprit, so its failures populate rows_needing_review.

Every check is skipped (reported as None) when its inputs are missing, and
nothing here raises: a statement with no header fields at all must degrade to
"could not verify", never to a traceback. See the crash-safety note on
reconcile().

THE SIGN FACTOR
---------------
Amounts are normalized; balances are not. On a credit account the printed
balance is a liability and moves opposite to the amount. All three checks
therefore apply a sign factor -- see the adapter.py module docstring for the
full rule. The header totals in check B are in amount space, not balance
space, so they take the same factor rather than the opposite one.
"""

import logging
import math

from adapter import TOTALS_DERIVED

logger = logging.getLogger(__name__)

# Statements round to the cent, so anything inside a cent is agreement. The
# epsilon keeps a difference of exactly 0.01 from failing on binary float
# representation (0.01 often materializes as 0.010000000000000231).
TOLERANCE = 0.01
_EPSILON = 1e-9

# Deposit accounts hold assets, credit accounts hold liabilities.
_LIABILITY_TYPES = frozenset({"credit"})


def balance_sign(account_type: str | None) -> int:
    """+1 where balance is an asset, -1 where it is a liability.

    The bridge between normalized amounts and the statement's own balances:
        balance_delta == amount * balance_sign(account_type)
    """
    return -1 if account_type in _LIABILITY_TYPES else 1


def _num(value) -> float | None:
    """Coerce to a finite float, or None. Never raises.

    Deliberately paranoid: this module's entire job is to be the thing that
    does not fall over when the extraction is wrong, and a parser that
    hallucinated a balance can just as easily hallucinate "N/A", None or NaN
    into a numeric field.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _agrees(left: float, right: float) -> bool:
    return abs(left - right) <= TOLERANCE + _EPSILON


def reconcile(account: dict, txns: list[dict]) -> dict:
    """Check an extraction against the statement's own arithmetic.

    Returns:
        {
          "reconciled": bool,            # every check that ran, passed
          "delta": float,                # check A's absolute difference
          "checks": {"sum_vs_balance": bool|None,
                     "totals_vs_balance": bool|None,
                     "running_balance": bool|None},
          "rows_needing_review": [int],  # indices into txns, from check C
          "checks_run": int,             # how many of the three had inputs
        }

    Crash safety: this function does not raise. Missing keys, null balances,
    an empty transaction list, a null account, and non-numeric junk in numeric
    fields all degrade to "check skipped" rather than an exception. A check
    that cannot run is None and is excluded from the verdict.
    """
    account = account if isinstance(account, dict) else {}
    txns = txns if isinstance(txns, list) else []

    sign = balance_sign(account.get("account_type"))
    opening = _num(account.get("opening_balance"))
    closing = _num(account.get("closing_balance"))

    # The change the statement claims its balance underwent.
    expected_delta = None if opening is None or closing is None else closing - opening

    checks: dict[str, bool | None] = {}

    # --- A: do the rows we extracted sum to that change? ------------------
    # Fails when a row was dropped or invented. delta is how much we are off
    # by, which for a single missing row is that row's amount.
    amounts = [a for a in (_num(t.get("amount")) for t in txns if isinstance(t, dict))
               if a is not None]
    delta = 0.0
    if expected_delta is None or not amounts:
        checks["sum_vs_balance"] = None
    else:
        observed = round(sum(amounts) * sign, 2)
        delta = round(abs(observed - expected_delta), 2)
        checks["sum_vs_balance"] = _agrees(observed, expected_delta)

    # --- B: do the statement's own printed totals agree? ------------------
    # Independent of our row extraction entirely -- it compares two header
    # figures against each other, so A passing and B failing means the bank's
    # summary disagrees with its own balance, not that we misread anything.
    #
    # Only runs on totals the bank actually printed. When the adapter had to
    # derive them from our own rows, deposits - withdrawals is identically
    # sum(amounts) and this check collapses into check A: it would agree
    # every time and add no evidence, while making checks_run read 3. A check
    # that cannot fail is not a check.
    deposits = _num(account.get("total_deposits"))
    withdrawals = _num(account.get("total_withdrawals"))
    printed = account.get("totals_source") != TOTALS_DERIVED

    if expected_delta is None or deposits is None or withdrawals is None or not printed:
        checks["totals_vs_balance"] = None
    else:
        checks["totals_vs_balance"] = _agrees((deposits - withdrawals) * sign, expected_delta)

    # --- C: does each row's printed balance move by its own amount? -------
    # `previous` is seeded with opening_balance so that row 0 is anchored
    # against it. Consecutive pairs alone never examine row 0, which makes it
    # the one row A can detect an error in but nothing can attribute it to:
    # the user is told the statement is off by $1842.55 and given no row to
    # look at. The anchor buys localization, not extra detection -- a
    # uniformly shifted statement stays self-consistent and no arithmetic
    # check can reject it. When opening_balance is null the seed is simply
    # None, row 0 is skipped, and this degrades to consecutive-pair checking.
    previous = opening
    rows_needing_review: list[int] = []
    comparisons = 0

    for index, txn in enumerate(txns):
        if not isinstance(txn, dict):
            continue
        balance = _num(txn.get("balance"))
        amount = _num(txn.get("amount"))

        if previous is not None and balance is not None and amount is not None:
            comparisons += 1
            if not _agrees(balance - previous, amount * sign):
                rows_needing_review.append(index)

        # Reassigned unconditionally: a row with no printed balance breaks the
        # chain on both sides, which is what "consecutive pair where both have
        # a balance" means. Carrying the last seen balance forward instead
        # would silently compare across a gap.
        previous = balance

    checks["running_balance"] = None if comparisons == 0 else not rows_needing_review

    # --- verdict ----------------------------------------------------------
    ran = [passed for passed in checks.values() if passed is not None]
    # DEVIATION from CLAUDE.md, which says reconciled is True if every
    # non-None check passed. With zero checks that is vacuously True, and the
    # dashboard would show a green "verified" badge for a statement where
    # nothing whatsoever was verified. Claiming verification we did not do is
    # the one failure this feature exists to prevent, so no evidence means
    # not reconciled. checks_run is exposed so the UI can tell "we checked and
    # it passed" apart from "there was nothing to check".
    reconciled = bool(ran) and all(ran)

    logger.info(
        "Reconcile %s (%s, sign %+d): %s | A=%s B=%s C=%s | delta %.2f | %d rows flagged",
        account.get("id"), account.get("account_type"), sign,
        "OK" if reconciled else "FAILED",
        checks["sum_vs_balance"], checks["totals_vs_balance"],
        checks["running_balance"], delta, len(rows_needing_review),
    )

    return {
        "reconciled": reconciled,
        "delta": delta,
        "checks": checks,
        "rows_needing_review": rows_needing_review,
        "checks_run": len(ran),
    }
