"""Recurring-charge detection. Stage 7.

Finds subscriptions by looking for a merchant charged on a regular cadence,
and reports when its price has moved. This is the feature the demo is built
around, so the bias throughout is against false positives: a subscription we
miss is invisible, but one we invent is on screen, wrong, in front of judges.

HOW A CADENCE IS MATCHED, AND WHY IT IS NOT CLAUDE.MD'S LITERAL RULE
--------------------------------------------------------------------
CLAUDE.md says: take the median gap, require std(gaps)/median(gaps) < 0.25,
then match the median to 7/14/30/90/365 within 15%. Its own Gym test case
fails that rule in both halves, which CLAUDE.md anticipates ("if it does not,
tune and document"). Measured:

    Gym 2026-01-05, 02-05, 04-05   gaps [31, 59]
    median 45, pstdev 14.00, ratio 0.311   -> judged irregular (> 0.25)
    median 45 matches no cadence           -> monthly is 25.5-34.5

The flaw is that a missed payment is treated as a change of rhythm. It is
not: a 59-day gap on a monthly subscription is one period plus one skipped
period. So each candidate cadence is tested by asking how many periods each
gap spans, and regularity is judged on the resulting PER-PERIOD gaps:

    cadence 30 -> periods [1, 2] -> per-period [31.0, 29.5] -> ratio 0.025

Candidates are then ranked by fewest skipped periods, not by cadence size.
Without that, [31, 28, 31] matches BIWEEKLY -- every other payment skipped is
a legal reading of a monthly charge, and testing cadences in ascending order
picks it. Preferring the simplest explanation fixes it, and is why Netflix
comes back as 30 rather than 14.

WHAT IS EXCLUDED, AND WHY
-------------------------
- Transfers. A card payment every month is regular, but it is money moving,
  not a subscription.
- Deposits. Only money leaving counts. Without this a fortnightly salary is a
  textbook biweekly "subscription" with an annual cost, which is both wrong
  and the single worst thing that could appear on that table on stage.
- Cheques and ATM withdrawals. Stage 4 correctly strips the varying number
  from "Cheque No. - 409/410/411", so three cheques share one merchant key
  and three is exactly the detection threshold. The number is noise for
  grouping and nothing is wrong upstream, so the exclusion belongs here.
"""

import csv
import logging
import re
import statistics
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

# --- what KIND of recurrence this is --------------------------------------
#
# Detection says a merchant recurs on a regular cadence. It cannot say
# whether the customer agreed to be billed or simply has a routine, and the
# two look statistically identical:
#
#     Netflix    every 30 days, 15.99 fixed   -> subscription
#     Starbucks  every  7 days,  5.25 fixed   -> habit
#
# The decisive question is whether it stops by cancelling something or only
# by changing behaviour. That is a fact about the merchant, so it resolves
# once and caches forever.
SUBSCRIPTION = "subscription"   # billed by prior agreement
BILL = "bill"                   # billed automatically, amount varies with use
HABITUAL = "habitual"           # chosen each time; regularity is a routine
UNCLEAR = "unclear"             # not recognised -- do not guess

NATURES = (SUBSCRIPTION, BILL, HABITUAL, UNCLEAR)

# Stage 5's category answers this for free on most merchants. Only the
# ambiguous ones need a model, which is why this layer exists first: a gym is
# Health but IS a subscription, insurance is Other but IS a bill.
NATURE_BY_CATEGORY = {
    "Subscriptions": SUBSCRIPTION,
    "Utilities": BILL,
    "Housing": BILL,
    "Food & Drink": HABITUAL,
    "Groceries": HABITUAL,
    "Transportation": HABITUAL,
    "Shopping": HABITUAL,
    # Deliberately absent -- genuinely ambiguous, resolved by the cache below
    # or left UNCLEAR: Health, Entertainment, Education, Travel, Other,
    # Fees & Interest, Income, Transfer.
}

# Written by scripts/classify_recurring_kinds.py. Absent file is fine.
KINDS_PATH = Path(__file__).resolve().parent / "data" / "recurring_kinds.csv"
KINDS_FIELDS = ("merchant", "nature", "source")

_kinds: dict[str, str] | None = None


def load_recurring_kinds(force: bool = False) -> dict[str, str]:
    """Merchant -> nature, from the reviewed cache. Missing file is not an error."""
    global _kinds
    if _kinds is not None and not force:
        return _kinds
    kinds: dict[str, str] = {}
    if KINDS_PATH.exists():
        with KINDS_PATH.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                merchant = (row.get("merchant") or "").strip()
                nature = (row.get("nature") or "").strip()
                if merchant and nature in NATURES:
                    kinds[merchant] = nature
    _kinds = kinds
    logger.info("Loaded %d merchant recurrence kinds", len(kinds))
    return _kinds


def classify_nature(merchant: str, category: str | None) -> str:
    """Category first (free, deterministic), then the reviewed cache.

    Defaults to UNCLEAR rather than guessing. UNCLEAR is routed to repeated
    spending, not subscriptions, because a fabricated subscription is on
    screen and wrong while an under-labelled habit is merely unremarkable.
    """
    by_category = NATURE_BY_CATEGORY.get((category or "").strip())
    if by_category:
        return by_category
    return load_recurring_kinds().get((merchant or "").strip(), UNCLEAR)

MIN_OCCURRENCES = 3
CADENCES = (7, 14, 30, 90, 365)
CADENCE_TOLERANCE = 0.15      # a gap may sit within +-15% of its cadence
GAP_REGULARITY_MAX = 0.25     # pstdev/median of per-period gaps
AMOUNT_REGULARITY_MAX = 0.15  # below this a subscription is "fixed"
MAX_PERIODS_PER_GAP = 3       # at most two consecutive payments missed
PRICE_CHANGE_MIN_PCT = 2.0    # smaller moves are rounding, not a price rise

# Regular, but not subscriptions. Matched against the normalized merchant.
NOT_A_SUBSCRIPTION = re.compile(
    r"^(cheque|check)\b|\bcheque no\b|atm withdrawal|cash withdrawal|"
    r"funds transfer|e-?transfer",
    re.I,
)


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


def _num(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and abs(result) != float("inf") else None


def match_cadence(gaps: list[float]) -> tuple[int, float] | None:
    """Best (cadence_days, regularity_ratio) for a list of day-gaps, or None.

    Ranked by fewest skipped periods first, then by regularity. See the module
    docstring for why that ordering is load-bearing.
    """
    if not gaps or any(g <= 0 for g in gaps):
        return None

    best = None
    for cadence in CADENCES:
        periods = [max(1, round(gap / cadence)) for gap in gaps]
        if any(p > MAX_PERIODS_PER_GAP for p in periods):
            continue

        per_period = [gap / p for gap, p in zip(gaps, periods)]
        if not all(abs(g - cadence) <= CADENCE_TOLERANCE * cadence for g in per_period):
            continue

        median = statistics.median(per_period)
        if not median:
            continue
        ratio = statistics.pstdev(per_period) / median
        if ratio >= GAP_REGULARITY_MAX:
            continue

        candidate = (sum(p - 1 for p in periods), ratio, cadence)
        if best is None or candidate < best:
            best = candidate

    return (best[2], best[1]) if best else None


def _eligible(txn: dict) -> bool:
    if not isinstance(txn, dict):
        return False
    if txn.get("is_transfer"):
        return False
    amount = _num(txn.get("amount"))
    if amount is None or amount >= 0:      # spending only
        return False
    if _as_date(txn.get("date")) is None:
        return False
    merchant = (txn.get("merchant") or "").strip()
    if not merchant or NOT_A_SUBSCRIPTION.search(merchant):
        return False
    return True


def find_recurring(txns: list[dict]) -> list[dict]:
    """Detect subscriptions and recurring bills across all accounts.

    Groups by normalized merchant regardless of account, because a
    subscription can move from one card to another and should not read as two
    separate short histories. Sets is_recurring=True on contributing rows.
    """
    if not isinstance(txns, list) or not txns:
        logger.info("Recurring detection: no transactions")
        return []

    for txn in txns:
        if isinstance(txn, dict):
            txn.setdefault("is_recurring", False)

    groups: dict[str, list[dict]] = {}
    for txn in txns:
        if _eligible(txn):
            groups.setdefault(txn["merchant"].strip(), []).append(txn)

    results = []
    for merchant, rows in groups.items():
        if len(rows) < MIN_OCCURRENCES:
            continue

        rows.sort(key=lambda t: _as_date(t["date"]))
        dates = [_as_date(t["date"]) for t in rows]
        amounts = [abs(_num(t["amount"])) for t in rows]

        gaps = [(dates[i] - dates[i - 1]).days for i in range(1, len(dates))]
        matched = match_cadence(gaps)
        if matched is None:
            continue
        cadence_days, _ = matched

        median_amount = statistics.median(amounts)
        mean_amount = statistics.mean(amounts)
        amount_ratio = statistics.pstdev(amounts) / mean_amount if mean_amount else 0.0
        kind = "fixed" if amount_ratio < AMOUNT_REGULARITY_MAX else "variable"

        # First to last, not min to max: a subscription that rose and fell
        # back has not changed price, and max/min would claim it had.
        change_pct = None
        if amounts[0]:
            raw = (amounts[-1] - amounts[0]) / amounts[0] * 100
            if abs(raw) >= PRICE_CHANGE_MIN_PCT:
                change_pct = round(raw, 1)

        for txn in rows:
            txn["is_recurring"] = True

        # Most common category across the group; ties break on the most
        # recent row, which is the freshest categorization.
        categories = Counter(t.get("category") for t in rows if t.get("category"))
        category = categories.most_common(1)[0][0] if categories else None

        results.append({
            "merchant": merchant,
            "amount": round(median_amount, 2),
            "cadence_days": cadence_days,
            "nature": classify_nature(merchant, category),
            "occurrences": len(rows),
            "last_seen": dates[-1].isoformat(),
            # cadence_days rather than the raw median gap, which CLAUDE.md
            # names. They agree unless a payment was skipped, and then the
            # raw median is inflated -- the Gym case would predict the next
            # charge 45 days out instead of 30.
            "annual_cost": round(median_amount * (365 / cadence_days), 2),
            "price_change_pct": change_pct,
            "first_seen": dates[0].isoformat(),
            "next_expected": (dates[-1] + timedelta(days=cadence_days)).isoformat(),
            "account_id": rows[-1].get("account_id") or "",
            "kind": kind,
        })

    results.sort(key=lambda r: (-r["annual_cost"], r["merchant"]))

    natures = Counter(r["nature"] for r in results)
    logger.info(
        "Recurring detection: %d recurring merchant(s) from %d candidate(s) | "
        "subscription %d | bill %d | habitual %d | unclear %d | %d price change(s)",
        len(results), len(groups), natures[SUBSCRIPTION], natures[BILL],
        natures[HABITUAL], natures[UNCLEAR],
        sum(1 for r in results if r["price_change_pct"]),
    )
    if natures[UNCLEAR]:
        logger.info(
            "Unresolved merchants routed to repeated spending: %s. Run "
            "scripts/classify_recurring_kinds.py to label them.",
            ", ".join(sorted(r["merchant"] for r in results if r["nature"] == UNCLEAR)),
        )
    return results


def split_recurring(results: list[dict]) -> tuple[list[dict], list[dict]]:
    """Partition find_recurring() output into (subscriptions, repeated_spending).

    Subscriptions and bills are things the user pays for; habits and
    unrecognised merchants are things the user buys. Only the first group
    answers "what am I signed up for?", which is what that table is for.
    """
    subscriptions, repeated = [], []
    for row in results or []:
        (subscriptions if row.get("nature") in (SUBSCRIPTION, BILL) else repeated).append(row)

    subscriptions.sort(key=lambda r: (-r["annual_cost"], r["merchant"]))
    repeated.sort(key=lambda r: (-r["annual_cost"], r["merchant"]))
    return subscriptions, repeated
