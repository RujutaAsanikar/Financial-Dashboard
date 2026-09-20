"""Cross-account transfer pairing. Stage 6.

When you pay your credit card, the statement shows it twice: money leaving
checking, and money arriving at the card. Both are real rows on real
statements, but only one movement happened, and counting them separately
inflates spending and income by the same amount. Every headline figure on the
dashboard is wrong until these are identified.

A pair is two transactions on DIFFERENT accounts whose amounts are equal and
opposite within a cent, dated within window_days of each other. Description
keywords ("PAYMENT", "ZELLE", ...) are a tiebreaker between otherwise equal
candidates -- never a requirement -- because plenty of genuine transfers are
described as nothing but a reference number.
"""

import logging
import math
from collections import defaultdict
from datetime import date, datetime

logger = logging.getLogger(__name__)

AMOUNT_TOLERANCE = 0.01
_EPSILON = 1e-9

# Tiebreakers only. A pair with none of these still pairs; a pair with one of
# these is preferred when two candidates are otherwise indistinguishable.
TRANSFER_KEYWORDS = (
    "PAYMENT", "TRANSFER", "THANK YOU", "XFER", "ZELLE", "VENMO",
    "ONLINE PMT", "AUTOPAY",
)


def _num(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _as_date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.strptime(value.strip(), "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def _has_keyword(*descriptions: str) -> bool:
    joined = " ".join(d.upper() for d in descriptions if isinstance(d, str))
    return any(keyword in joined for keyword in TRANSFER_KEYWORDS)


def find_transfer_pairs(txns: list[dict], window_days: int = 3) -> list[tuple[int, int]]:
    """Indices of transactions that pair off as transfers.

    Every transaction appears in at most one pair. Candidates are ranked
    globally rather than matched greedily in list order, so the result does
    not depend on how the statements happened to be concatenated -- the same
    set of transactions always produces the same pairing.
    """
    if not isinstance(txns, list) or len(txns) < 2:
        return []

    # Bucket by absolute amount so this is not O(n^2) over everything. The
    # tolerance is a cent, so a candidate can also sit in the neighbouring
    # cent bucket; those are checked explicitly rather than widening the key.
    buckets: dict[float, list[int]] = defaultdict(list)
    for index, txn in enumerate(txns):
        if not isinstance(txn, dict):
            continue
        amount = _num(txn.get("amount"))
        if amount is None or amount == 0:
            continue
        if _as_date(txn.get("date")) is None:
            continue
        buckets[round(abs(amount), 2)].append(index)

    candidates: list[tuple[int, int, int, int]] = []   # (-keyword, gap, i, j)
    considered: set[tuple[int, int]] = set()

    for key, indices in buckets.items():
        partners = list(indices)
        # Only look one bucket up, so each pair is generated once.
        partners += buckets.get(round(key + AMOUNT_TOLERANCE, 2), [])

        for position, left in enumerate(indices):
            for right in partners[position + 1:]:
                if left == right:
                    continue
                pair = (left, right) if left < right else (right, left)
                if pair in considered:
                    continue
                considered.add(pair)

                first, second = txns[pair[0]], txns[pair[1]]

                if first.get("account_id") == second.get("account_id"):
                    continue

                left_amount = _num(first.get("amount"))
                right_amount = _num(second.get("amount"))
                # Equal and opposite: the sum of a true pair cancels out.
                if left_amount * right_amount >= 0:
                    continue
                if abs(left_amount + right_amount) > AMOUNT_TOLERANCE + _EPSILON:
                    continue

                gap = abs((_as_date(first.get("date")) - _as_date(second.get("date"))).days)
                if gap > window_days:
                    continue

                keyword = _has_keyword(first.get("description") or "",
                                       second.get("description") or "")
                candidates.append((0 if keyword else 1, gap, pair[0], pair[1]))

    # Keyword pairs first, then the closest dates, then index order so the
    # outcome is fully determined by the data.
    candidates.sort()

    used: set[int] = set()
    pairs: list[tuple[int, int]] = []
    for _, _, left, right in candidates:
        if left in used or right in used:
            continue
        used.add(left)
        used.add(right)
        pairs.append((left, right))

    logger.info("Transfer detection: %d pair(s) from %d transactions", len(pairs), len(txns))
    return pairs


def mark_transfers(txns: list[dict], window_days: int = 3) -> list[dict]:
    """Set is_transfer=True on both sides of every detected pair."""
    if not isinstance(txns, list):
        return txns

    for txn in txns:
        if isinstance(txn, dict):
            txn.setdefault("is_transfer", False)

    pairs = find_transfer_pairs(txns, window_days=window_days)
    total = 0.0
    for left, right in pairs:
        txns[left]["is_transfer"] = True
        txns[right]["is_transfer"] = True
        total += abs(_num(txns[left].get("amount")) or 0.0)

    if pairs:
        logger.info("Marked %d rows as transfers, %.2f excluded from spending",
                    2 * len(pairs), total)
    return txns
