"""Merchant -> category, via a three-layer cascade. Stage 5.

CLAUDE.md's structure, with Triqai standing in for Gemini at layer 3:

    layer 1   data/merchant_dict.csv      exact match, confidence 1.0
    layer 2   merchant_cache in DuckDB    exact match, confidence 0.9
    layer 3   Triqai alias cache, then a live call if enabled     0.7
    failure   "Other"                                             0.0

The layer breakdown is logged at INFO because it is the claim the project
rests on: layers 1 and 2 are plain dictionary lookups, so a high hit rate
there is the evidence that this is not an LLM wrapper. CLAUDE.md sets the bar
at 70% for layers 1+2 combined.

Layer 3 never blocks the pipeline. No key, no network, a timeout, an
unmappable category -- all of it lands on "Other" at confidence 0.0 and
processing continues.
"""

import logging

import db
import normalize as N
import triq

logger = logging.getLogger(__name__)

# The only category strings allowed anywhere downstream. CLAUDE.md fixes this
# list; models.py, the dashboard and the text-to-SQL prompt all assume it.
CATEGORIES = (
    "Food & Drink", "Groceries", "Transportation", "Shopping", "Entertainment",
    "Subscriptions", "Utilities", "Housing", "Health", "Education", "Travel",
    "Income", "Transfer", "Fees & Interest", "Other",
)

FALLBACK_CATEGORY = "Other"

# Triqai's taxonomy is finer than ours and is its own vocabulary, so it has to
# be translated rather than trusted. Every value on the right must be in
# CATEGORIES; a test asserts that.
#
# Two judgement calls worth naming:
#   - Streaming and creative software map to Subscriptions rather than
#     Entertainment. CLAUDE.md offers both, and a user reading the dashboard
#     expects Netflix to sit with their other recurring charges. Stage 7
#     detects subscriptions independently by cadence, so this does not affect
#     that feature either way.
#   - Cash & ATM maps to Transfer, not Other. Withdrawing cash moves money
#     rather than spending it, and lumping it into Other would inflate the
#     one bucket that is supposed to mean "we do not know".
TRIQ_CATEGORY_MAP = {
    "Cafes": "Food & Drink",
    "Restaurants": "Food & Drink",
    "Food Delivery": "Food & Drink",
    "Groceries": "Groceries",
    "Department Stores": "Shopping",
    "Online Marketplaces": "Shopping",
    "Home Improvement": "Shopping",
    "Ride-sharing": "Transportation",
    "Fuel": "Transportation",
    "Video Streaming": "Subscriptions",
    "Music Streaming": "Subscriptions",
    "Design & Creative Software": "Subscriptions",
    "Gaming": "Entertainment",
    "Online Learning": "Education",
    "Banking": "Fees & Interest",
    "Cash & ATM": "Transfer",
    "Savings Transfer": "Transfer",
    "Utilities": "Utilities",
    "Fitness": "Health",
    "Professional Services": FALLBACK_CATEGORY,
    "Uncategorized": FALLBACK_CATEGORY,
}

CONFIDENCE = {"dict": 1.0, "cache": 0.9, "triqai": 0.7, "fallback": 0.0}


def map_triq_category(name: str | None) -> str | None:
    """Translate one Triqai category into ours, or None if we cannot."""
    if not name:
        return None
    mapped = TRIQ_CATEGORY_MAP.get(name.strip())
    if mapped is None:
        logger.warning("Unmapped Triqai category %r; add it to TRIQ_CATEGORY_MAP", name)
        return None
    return None if mapped == FALLBACK_CATEGORY else mapped


# --- layer 1 ---------------------------------------------------------------

_dict_cache: dict[str, str] | None = None


def load_merchant_dict(force: bool = False) -> dict[str, str]:
    """data/merchant_dict.csv as {merchant: category}. Missing file is fine."""
    global _dict_cache
    if _dict_cache is not None and not force:
        return _dict_cache

    import csv
    path = N.ALIAS_PATH.parent / "merchant_dict.csv"
    entries: dict[str, str] = {}
    if path.exists():
        with path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                merchant = (row.get("merchant") or "").strip()
                category = (row.get("category") or "").strip()
                if merchant and category in CATEGORIES:
                    entries[merchant] = category
    _dict_cache = entries
    logger.info("Loaded %d merchants from merchant_dict.csv", len(entries))
    return entries


# --- layer 2 ---------------------------------------------------------------

def read_merchant_cache(merchants: set[str]) -> dict[str, str]:
    """Categories already learned, from the DuckDB merchant_cache table."""
    if not merchants:
        return {}
    try:
        with db.get_con(read_only=True) as con:
            rows = con.execute(
                "SELECT merchant, category FROM merchant_cache WHERE merchant IN "
                f"({', '.join('?' for _ in merchants)})",
                list(merchants),
            ).fetchall()
        return {m: c for m, c in rows if c in CATEGORIES}
    except Exception as exc:
        logger.warning("merchant_cache unreadable (%s); skipping layer 2", exc)
        return {}


def write_merchant_cache(learned: dict[str, str]) -> None:
    """Persist layer 3 answers so they never cost a second call."""
    if not learned:
        return
    try:
        with db.get_con() as con:
            con.executemany(
                "INSERT INTO merchant_cache (merchant, category) VALUES (?, ?) "
                "ON CONFLICT (merchant) DO NOTHING",
                list(learned.items()),
            )
        logger.info("Cached %d newly learned merchant categories", len(learned))
    except Exception as exc:
        logger.warning("Could not write merchant_cache (%s); categories not persisted", exc)


# --- layer 3 ---------------------------------------------------------------

def _category_from_triqai(raw_description: str, merchant: str,
                          allow_network: bool) -> str | None:
    """Alias cache first -- already paid for -- then a live call if enabled."""
    cached = N.load_aliases().get((raw_description or "").strip())
    if cached is not None:
        return map_triq_category(cached.get("category"))

    if not allow_network:
        return None

    result = triq.lookup(raw_description or merchant)
    if result.get("error"):
        return None
    return map_triq_category(result.get("category"))


# --- the cascade -----------------------------------------------------------

def categorize(txns: list[dict], *, allow_network: bool | None = None) -> list[dict]:
    """Assign "category" and "confidence" to every transaction, in place.

    Expects "merchant" to be set by Stage 4; falls back to normalizing the
    description if it is not, so this is safe to call on raw adapter output.
    """
    if not txns:
        logger.info("Categorize: nothing to do")
        return txns

    import os
    if allow_network is None:
        allow_network = os.getenv(N.LIVE_LOOKUP_ENV, "").lower() in ("1", "true", "yes")

    merchant_dict = load_merchant_dict()

    for txn in txns:
        if not txn.get("merchant"):
            txn["merchant"] = N.normalize(txn.get("description") or "")

    merchants = {t["merchant"] for t in txns if t.get("merchant")}
    cached = read_merchant_cache(merchants - set(merchant_dict))

    counts = {"dict": 0, "cache": 0, "triqai": 0, "fallback": 0}
    learned: dict[str, str] = {}

    for txn in txns:
        merchant = txn.get("merchant") or ""

        category = merchant_dict.get(merchant)
        layer = "dict"

        if category is None:
            category = cached.get(merchant) or learned.get(merchant)
            layer = "cache"

        if category is None:
            category = _category_from_triqai(txn.get("description") or "",
                                             merchant, allow_network)
            layer = "triqai"
            if category is not None and merchant:
                learned[merchant] = category

        if category is None:
            category = FALLBACK_CATEGORY
            layer = "fallback"

        txn["category"] = category
        txn["confidence"] = CONFIDENCE[layer]
        counts[layer] += 1

    write_merchant_cache(learned)

    total = len(txns)
    deterministic = counts["dict"] + counts["cache"]
    logger.info(
        "Categorize %d rows | dict %d (%.0f%%) | cache %d (%.0f%%) | "
        "triqai %d (%.0f%%) | other %d (%.0f%%) | layers 1+2 = %.0f%%",
        total,
        counts["dict"], 100 * counts["dict"] / total,
        counts["cache"], 100 * counts["cache"] / total,
        counts["triqai"], 100 * counts["triqai"] / total,
        counts["fallback"], 100 * counts["fallback"] / total,
        100 * deterministic / total,
    )
    if 100 * deterministic / total < 70:
        logger.warning(
            "Layers 1+2 cover only %.0f%% (<70%%). The dictionary is too small -- "
            "rebuild it with scripts/build_merchant_dict.py.",
            100 * deterministic / total,
        )
    return txns
