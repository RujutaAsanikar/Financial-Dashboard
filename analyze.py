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
import math
import os
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

    Offline and total. Defaults to UNCLEAR rather than guessing. UNCLEAR is
    routed to repeated spending, not subscriptions, because a fabricated
    subscription is on screen and wrong while an under-labelled habit is
    merely unremarkable.
    """
    by_category = NATURE_BY_CATEGORY.get((category or "").strip())
    if by_category:
        return by_category
    return load_recurring_kinds().get((merchant or "").strip(), UNCLEAR)


# --- live fallback ---------------------------------------------------------
#
# Opt-in. Tests never set the flag, so the suite is offline by construction
# rather than by discipline. Every failure mode lands on UNCLEAR, which is a
# usable answer, so the pipeline cannot be broken by the network.

LIVE_CLASSIFY_ENV = "CLAUDE_LIVE_CLASSIFY"

# One batched call per process, capped. The batch shape matters: classifying
# per merchant would put N sequential round-trips on an upload request.
MAX_LIVE_CLASSIFICATIONS = int(os.getenv("CLAUDE_MAX_LIVE_CLASSIFICATIONS", "40"))
LIVE_MODEL = "claude-opus-4-8"
LIVE_TIMEOUT_SECONDS = 20

# Shared with scripts/classify_recurring_kinds.py so the offline batch and the
# live fallback cannot drift apart and start labelling the same merchant
# differently.
CLASSIFY_SYSTEM = """\
You classify merchants by HOW a charge recurs, not whether it recurs. A
deterministic system has already confirmed each one recurs on a regular
cadence. Do not second-guess that. Your only job is the kind.

  subscription  Billed automatically by prior agreement. The customer signed
                up once and money leaves until they cancel. Netflix, Spotify,
                a gym membership, software seats, insurance premiums.

  bill          Billed automatically but the amount varies with usage, and
                stopping means losing the service rather than cancelling a
                plan. Electricity, water, gas, phone, internet.

  habitual      The customer chooses to buy each time. Regular timing reflects
                a routine, not an agreement. Coffee, groceries, lunch, fuel,
                transit fares. Cancelling is not a concept here.

  unclear       You do not recognise the merchant, or the name is too generic
                to tell. Prefer this over guessing.

The decisive test: could the customer stop this by cancelling something, or
only by changing their behaviour? Cancel -> subscription or bill. Behaviour
-> habitual.

Rules:
- Generic descriptors ("SUPERMARKET", "PHARMACY", "BOOKSTORE") are not brands.
  Return unclear rather than guessing which chain it might be.
- Judge the merchant, not the cadence. A weekly subscription is still a
  subscription; a monthly grocery run is still habitual.
- A fee, interest charge, or cheque is never any of the three: unclear.
- Echo each merchant string back exactly as given."""

_live_calls = 0


def live_classifications_used() -> int:
    return _live_calls


def _append_kinds(learned: dict[str, str]) -> None:
    """Persist live answers so the network is consulted once per merchant, ever."""
    if not learned:
        return
    KINDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    new_file = not KINDS_PATH.exists()
    with KINDS_PATH.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=KINDS_FIELDS)
        if new_file:
            writer.writeheader()
        for merchant, nature in sorted(learned.items()):
            writer.writerow({"merchant": merchant, "nature": nature, "source": "live"})
    load_recurring_kinds(force=True)


def classify_live(merchants: list[str]) -> dict[str, str]:
    """One batched call. Returns only merchants it could label; never raises.

    Answers of "unclear" are omitted rather than stored, so a later run can
    try again once the merchant has a real name behind it.
    """
    if not merchants:
        return {}

    try:
        import anthropic
        from pydantic import BaseModel
    except ImportError:
        logger.warning("anthropic is not installed; skipping live classification")
        return {}

    class _Kind(BaseModel):
        merchant: str
        nature: str

    class _Result(BaseModel):
        merchants: list[_Kind]

    try:
        client = anthropic.Anthropic(timeout=LIVE_TIMEOUT_SECONDS, max_retries=1)
        response = client.messages.parse(
            model=LIVE_MODEL,
            max_tokens=4096,
            # A short per-merchant judgement, not a reasoning problem.
            output_config={"effort": "low"},
            system=CLASSIFY_SYSTEM,
            messages=[{"role": "user",
                       "content": "Merchants:\n" + "\n".join(merchants)}],
            output_format=_Result,
        )
    except Exception as exc:
        logger.warning("Live classification failed (%s: %s); %d merchant(s) stay "
                       "in repeated spending", type(exc).__name__, exc, len(merchants))
        return {}

    requested = set(merchants)
    learned: dict[str, str] = {}
    for item in response.parsed_output.merchants:
        nature = (item.nature or "").strip().lower()
        # Only accept labels for merchants we actually asked about -- a
        # hallucinated extra row must not enter the cache.
        if item.merchant in requested and nature in NATURES and nature != UNCLEAR:
            learned[item.merchant] = nature

    logger.info("Live classification: %d/%d merchant(s) labelled",
                len(learned), len(merchants))
    return learned


def classify_natures(pairs: list[tuple[str, str | None]], *,
                     allow_network: bool | None = None) -> dict[str, str]:
    """Resolve many (merchant, category) pairs at once.

    Category -> cache -> one batched live call -> UNCLEAR. Batching is the
    point: a live call per merchant would put N sequential round-trips on the
    upload request.
    """
    global _live_calls

    natures = {merchant: classify_nature(merchant, category)
               for merchant, category in pairs}

    unresolved = sorted(m for m, n in natures.items() if n == UNCLEAR and m)
    if not unresolved:
        return natures

    if allow_network is None:
        allow_network = os.getenv(LIVE_CLASSIFY_ENV, "").lower() in ("1", "true", "yes")
    if not allow_network:
        return natures

    if _live_calls >= MAX_LIVE_CLASSIFICATIONS:
        logger.warning("Live classification budget of %d exhausted; %d merchant(s) "
                       "stay in repeated spending",
                       MAX_LIVE_CLASSIFICATIONS, len(unresolved))
        return natures

    batch = unresolved[:MAX_LIVE_CLASSIFICATIONS - _live_calls]
    _live_calls += len(batch)

    learned = classify_live(batch)
    _append_kinds(learned)
    natures.update(learned)
    return natures

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


def find_recurring(txns: list[dict], *, allow_network: bool | None = None) -> list[dict]:
    """Detect subscriptions and recurring bills across all accounts.

    Groups by normalized merchant regardless of account, because a
    subscription can move from one card to another and should not read as two
    separate short histories. Sets is_recurring=True on contributing rows.

    allow_network defaults to the CLAUDE_LIVE_CLASSIFY environment variable
    and controls only the subscription/habitual labelling -- detection itself
    is always deterministic and never touches the network.
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
            # Filled in by one batched pass below, not per merchant.
            "nature": None,
            "_category": category,
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

    # One resolution pass for every merchant at once. If the live fallback is
    # enabled, this is the single call for the whole upload.
    resolved = classify_natures(
        [(r["merchant"], r.pop("_category")) for r in results],
        allow_network=allow_network,
    )
    for row in results:
        row["nature"] = resolved.get(row["merchant"], UNCLEAR)

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


# --- payoff amortization. Stage 8 ------------------------------------------
#
# The minimum payment is interest + 1% of principal, not CLAUDE.md's
# max(25, 2% of balance). The spec's formula does not amortize the demo card:
#
#     $3,204.18 at 24.99%  ->  2% is $64.08, one month's interest is $66.73
#
# and the payoff chart renders empty. It is not a rounding problem -- a flat
# percentage falls below interest whenever APR exceeds 12x that percentage,
# so 2% breaks above 24% APR and 3% breaks above 36%. The demo card sits just
# past the first cliff. Interest + 1% of principal is above interest by
# construction at any APR, and it is what card issuers actually use, so it
# survives being asked where the number came from.
MINIMUM_PAYMENT_FLOOR = 25.0
MINIMUM_PRINCIPAL_FRACTION = 0.01
MAX_PAYOFF_MONTHS = 600

# Balances are carried to the cent, as a statement does. Without this a float
# residue of ~1e-11 dollars survives the final payment and the loop bills an
# extra month: a payment computed to clear in exactly 36 months reported 37.
CENTS = 2


def minimum_payment(balance: float, apr: float) -> float:
    """One month's interest plus 1% of principal, floored at $25."""
    balance = max(0.0, _num(balance) or 0.0)
    apr = max(0.0, _num(apr) or 0.0)
    interest = balance * (apr / 100 / 12)
    return round(max(MINIMUM_PAYMENT_FLOOR, interest + MINIMUM_PRINCIPAL_FRACTION * balance), 2)


def payment_for_months(balance: float, apr: float, months: int) -> float:
    """The monthly payment that clears `balance` in exactly `months`.

    The inverse of the amortization loop, in closed form:
        P = B*r / (1 - (1+r)^-n)
    At 0% APR this degenerates to B/n, which the formula cannot express
    (division by zero), so that case is handled separately.

    Rounded UP to the cent, never to nearest. A payment a fraction of a cent
    short does not clear the balance and the amortization bills an extra
    month: $304.5232 rounds to $304.52 and pays off in 13 months, not 12.
    Rounding up is also what the question asks for -- the payment needed to
    finish in n months cannot be less than the payment that finishes in n
    months.
    """
    balance = max(0.0, _num(balance) or 0.0)
    apr = max(0.0, _num(apr) or 0.0)
    months = max(1, int(months))
    if balance <= 0:
        return 0.0
    rate = apr / 100 / 12
    exact = balance / months if rate == 0 else balance * rate / (1 - (1 + rate) ** -months)
    return math.ceil(exact * 100) / 100


def calculate_payoff(balance: float, apr: float, monthly_payment: float) -> dict | None:
    """Amortize a balance. None when the payment never clears it.

    Returns {"months", "total_interest", "series"}. `series` is the balance
    remaining after each payment, so the last point is 0.
    """
    balance = _num(balance)
    apr = _num(apr)
    monthly_payment = _num(monthly_payment)
    if balance is None or apr is None or monthly_payment is None:
        return None
    if balance <= 0:
        return {"months": 0, "total_interest": 0.0, "series": []}
    if monthly_payment <= 0 or apr < 0:
        return None

    rate = apr / 100 / 12
    # A payment that does not cover the first month's interest never reduces
    # the principal, so the balance grows forever.
    if monthly_payment <= round(balance * rate, CENTS):
        return None

    remaining = balance
    total_interest = 0.0
    series: list[dict] = []

    for month in range(1, MAX_PAYOFF_MONTHS + 1):
        interest = round(remaining * rate, CENTS)
        total_interest += interest
        remaining = round(remaining + interest - monthly_payment, CENTS)
        if remaining < 0:
            remaining = 0.0
        series.append({"month": month, "balance": remaining})
        if remaining <= 0:
            return {"months": month,
                    "total_interest": round(total_interest, CENTS),
                    "series": series}

    # Still owing after 50 years. Treated the same as a payment below
    # interest: it does not pay off, so there is nothing to chart.
    logger.warning("Payoff exceeded %d months at %.2f/mo on %.2f at %.2f%%; "
                   "reporting as never paid off",
                   MAX_PAYOFF_MONTHS, monthly_payment, balance, apr)
    return None


def payoff_scenarios(balance: float, apr: float) -> list[dict]:
    """Four scenarios, ordered from least to most aggressive.

    The first two mirror the minimum-payment disclosure US card statements
    have been required to print since the CARD Act: what the minimum costs,
    and what clearing the balance in three years costs instead. The other two
    are levers the user can actually pull.

    Anything that does not amortize is dropped rather than returned as a null
    row, so the chart never has to render an empty line.
    """
    balance = _num(balance)
    apr = _num(apr)
    if balance is None or apr is None or balance <= 0:
        return []

    minimum = minimum_payment(balance, apr)
    candidates = [
        ("Minimum only", minimum),
        ("Pay off in 3 years", payment_for_months(balance, apr, 36)),
        ("Minimum + $100", round(minimum + 100, 2)),
        ("Pay off in 1 year", payment_for_months(balance, apr, 12)),
    ]

    scenarios = []
    seen: set[float] = set()
    for label, payment in candidates:
        # Two rules can land on near-identical payments; a duplicate line on
        # the chart is noise.
        if payment <= 0 or any(abs(payment - p) < 1.0 for p in seen):
            continue
        result = calculate_payoff(balance, apr, payment)
        if result is None:
            logger.info("Scenario %r (%.2f/mo) never pays off; omitted", label, payment)
            continue
        seen.add(payment)
        scenarios.append({"label": label, "monthly_payment": payment, **result})

    scenarios.sort(key=lambda s: s["monthly_payment"])
    return scenarios


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
