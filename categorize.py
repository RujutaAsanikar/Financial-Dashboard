"""Merchant -> category, via a cascade with Triqai and Anthropic as a
cross-check when Triqai isn't confident. Stage 5.

    layer 1   data/merchant_dict.csv        exact match, confidence 1.0
    layer 2   merchant_cache in DuckDB      exact match, confidence 0.9
    layer 3   live keyword rules            regex match, confidence 0.95
    layer 4   Triqai                        confidence 0.85, ONLY when
                                             Triqai's own reported confidence
                                             is >= 90
    layer 5   Anthropic arbitration         when Triqai is unsure (<90) or
                                             errored, one batched call covers
                                             every such transaction in the
                                             upload and gives an independent
                                             opinion:
                                               - agrees with Triqai  -> 0.85
                                               - disagrees           -> 0.75,
                                                 Anthropic's answer wins (it
                                                 was only consulted because
                                                 Triqai was already unsure)
                                               - Anthropic unreachable
                                                 too -> fall back to
                                                 Triqai's low-confidence
                                                 guess at 0.5, or "Other" if
                                                 Triqai had nothing
    failure   "Other"                                                  0.0

The layer breakdown is logged at INFO because it is the claim the project
rests on: layers 1, 2 and 3 are plain lookups/regexes, so a high hit rate
there is the evidence that this is not an LLM wrapper. CLAUDE.md sets the bar
at 70% for that combined coverage.

Layers 4 and 5 never block the pipeline. No key, no network, a timeout, an
unmappable category, a malformed model response -- all of it degrades to the
next layer down and processing continues.
"""

import logging
import re

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

CONFIDENCE = {
    "dict": 1.0,
    "keyword": 0.95,
    "cache": 0.9,
    "triqai": 0.85,
    "agreed": 0.85,
    "anthropic": 0.75,
    "triqai_low": 0.5,
    "fallback": 0.0,
}

# Triqai's own confidence (0-100) below which its answer is not trusted
# outright and gets a second, independent opinion instead.
TRIQAI_TRUST_FLOOR = 90


def map_triq_category(name: str | None) -> str | None:
    """Translate one Triqai category into ours, or None if we cannot."""
    if not name:
        return None
    mapped = TRIQ_CATEGORY_MAP.get(name.strip())
    if mapped is None:
        logger.warning("Unmapped Triqai category %r; add it to TRIQ_CATEGORY_MAP", name)
        return None
    return None if mapped == FALLBACK_CATEGORY else mapped


# --- layer 1 -----------------------------------------------------------

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


# --- layer 2 -----------------------------------------------------------

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
    """Persist newly resolved answers so they never cost a second lookup."""
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


# --- layer 3: live keyword rules -----------------------------------------
#
# Applied to the raw description. Also the source data for
# scripts/build_merchant_dict.py, which imports from_keywords from here to
# build the offline dictionary -- this module owns the rules, the script
# just reuses them at build time.
#
# Applied in order; first match wins. Deliberately narrow -- a keyword that
# could belong to two categories is left out rather than guessed, because
# "Other" is honest and a wrong category is not. (This is why there is no
# bare "mobile" keyword: "Mobile Deposit" and "T-Mobile bill" are both real,
# and mean different things.)
KEYWORD_RULES: tuple[tuple[str, str], ...] = (
    (r"payroll|salary|direct deposit", "Income"),
    (r"interest charge|overdraft|annual membership fee|\bfees?\b|\bnsf\b|service charge",
     "Fees & Interest"),
    (r"funds transfer|transfer to|\bxfer\b|e-?transfer", "Transfer"),
    (r"atm withdrawal|cash withdrawal", "Transfer"),
    (r"automatic payment|payment - thank you|bill payment - visa|"
     r"bill payment - mastercard|bill payment - amex", "Transfer"),
    (r"mortgage|\brent\b|property tax|condo fee", "Housing"),
    (r"hydro|electric|\bgas bill\b|water bill|internet|"
     r"\bcable\b|telecom|wireless", "Utilities"),
    (r"pharmacy|drug ?mart|dental|clinic|medical|optical", "Health"),
    (r"\bgym\b|fitness|yoga", "Health"),
    (r"supermarket|grocer|market\b", "Groceries"),
    (r"restaurant|coffee|cafe|café|bistro|noodle|pizza|bakery", "Food & Drink"),
    (r"gas bar|petro|fuel|\bshell\b|esso|chevron", "Transportation"),
    (r"transit|parking|railway|\bbus\b|airline|airport", "Transportation"),
    (r"bookstore|hardware|electronics|clothing|department", "Shopping"),
    (r"tuition|university|college|course", "Education"),
    (r"insurance", "Other"),  # health vs home is unknowable here; do not guess
)

_COMPILED_KEYWORDS = tuple((re.compile(p, re.I), c) for p, c in KEYWORD_RULES)


def from_keywords(description: str) -> str | None:
    for pattern, category in _COMPILED_KEYWORDS:
        if pattern.search(description or ""):
            return None if category == "Other" else category
    return None


# --- layer 4: Triqai -----------------------------------------------------

def _triqai_lookup(raw_description: str, merchant: str,
                    allow_network: bool) -> tuple[str | None, float | None]:
    """Alias cache first -- already paid for -- then a live call if enabled.

    Returns (mapped_category_or_None, triqai_own_confidence_or_None). The
    confidence is Triqai's own 0-100 number, not ours -- the caller decides
    whether it clears TRIQAI_TRUST_FLOOR.
    """
    cached = N.load_aliases().get((raw_description or "").strip())
    if cached is not None:
        raw_confidence = cached.get("confidence")
        try:
            confidence = float(raw_confidence) if raw_confidence not in (None, "") else None
        except (TypeError, ValueError):
            confidence = None
        return map_triq_category(cached.get("category")), confidence

    if not allow_network:
        return None, None

    result = triq.lookup(raw_description or merchant)
    if result.get("error"):
        return None, None
    return map_triq_category(result.get("category")), result.get("confidence")


# --- layer 5: Anthropic arbitration ---------------------------------------

MODEL = "claude-opus-4-8"
TIMEOUT_SECONDS = 20
ARBITRATION_CHUNK_SIZE = 75


def _client():
    import anthropic
    return anthropic.Anthropic(timeout=TIMEOUT_SECONDS, max_retries=1)


def _arbitrate_batch(candidates: list[dict]) -> dict[int, str]:
    """One Anthropic call per chunk of candidates needing a second opinion.

    candidates: [{"index": int, "description": str, "triqai_hint": str|None}]
    Returns {index: category}. An index missing from the result means this
    layer could not resolve it; the caller falls back to Triqai's guess (if
    any) or "Other". Never raises -- a failure on one chunk just leaves that
    chunk's indices absent, the rest of the batch still gets a real answer.
    """
    if not candidates:
        return {}

    results: dict[int, str] = {}
    for start in range(0, len(candidates), ARBITRATION_CHUNK_SIZE):
        chunk = candidates[start:start + ARBITRATION_CHUNK_SIZE]
        try:
            results.update(_arbitrate_chunk(chunk))
        except Exception as exc:
            logger.warning("Anthropic arbitration failed for a chunk of %d: %s",
                          len(chunk), exc)
    return results


def _arbitrate_chunk(chunk: list[dict]) -> dict[int, str]:
    from pydantic import BaseModel

    class Item(BaseModel):
        index: int
        category: str

    class Arbitration(BaseModel):
        items: list[Item]

    lines = []
    for c in chunk:
        hint = f" (a different system guessed: {c['triqai_hint']})" if c["triqai_hint"] else ""
        lines.append(f"{c['index']}: {c['description']!r}{hint}")

    prompt = (
        "Categorize each bank transaction description below into EXACTLY one "
        "of these categories:\n"
        f"{', '.join(CATEGORIES)}\n\n"
        "The parenthetical hint after some lines is another system's guess -- "
        "it may be wrong; use your own judgement, it is not authoritative.\n\n"
        "Transactions:\n" + "\n".join(lines) +
        "\n\nReturn one item per transaction index, using only the categories listed."
    )

    response = _client().messages.parse(
        model=MODEL, max_tokens=4096,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}],
        output_format=Arbitration,
    )
    out: dict[int, str] = {}
    for item in response.parsed_output.items:
        if item.category in CATEGORIES:
            out[item.index] = item.category
    return out


# --- the cascade -----------------------------------------------------------

def categorize(txns: list[dict], *, allow_network: bool | None = None) -> list[dict]:
    """Assign "category" and "confidence" to every transaction, in place.

    Expects "merchant" to be set by Stage 4; falls back to normalizing the
    description if it is not, so this is safe to call on raw adapter output.

    allow_network gates BOTH live layers (Triqai and Anthropic arbitration)
    -- off means the whole cascade stays on dict/cache/keyword rules only.
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

    counts = {k: 0 for k in CONFIDENCE}
    learned: dict[str, str] = {}
    needs_arbitration: list[dict] = []

    for txn in txns:
        merchant = txn.get("merchant") or ""

        category = merchant_dict.get(merchant)
        layer = "dict"

        if category is None:
            category = cached.get(merchant) or learned.get(merchant)
            layer = "cache"

        if category is None:
            category = from_keywords(txn.get("description") or "")
            layer = "keyword"

        if category is not None:
            txn["category"] = category
            txn["confidence"] = CONFIDENCE[layer]
            counts[layer] += 1
            if layer == "keyword" and merchant:
                learned[merchant] = category
            continue

        triqai_category, triqai_confidence = _triqai_lookup(
            txn.get("description") or "", merchant, allow_network)

        if triqai_category is not None and (triqai_confidence or 0) >= TRIQAI_TRUST_FLOOR:
            txn["category"] = triqai_category
            txn["confidence"] = CONFIDENCE["triqai"]
            counts["triqai"] += 1
            if merchant:
                learned[merchant] = triqai_category
            continue

        needs_arbitration.append({
            "txn": txn, "merchant": merchant,
            "description": txn.get("description") or "",
            "triqai_category": triqai_category,
        })

    arbitrated: dict[int, str] = {}
    if needs_arbitration and allow_network:
        arbitrated = _arbitrate_batch([
            {"index": i, "description": c["description"], "triqai_hint": c["triqai_category"]}
            for i, c in enumerate(needs_arbitration)
        ])

    for i, c in enumerate(needs_arbitration):
        txn = c["txn"]
        anthropic_category = arbitrated.get(i)
        triqai_category = c["triqai_category"]

        if anthropic_category is not None and anthropic_category == triqai_category:
            category, layer = anthropic_category, "agreed"
        elif anthropic_category is not None:
            category, layer = anthropic_category, "anthropic"
        elif triqai_category is not None:
            category, layer = triqai_category, "triqai_low"
        else:
            category, layer = FALLBACK_CATEGORY, "fallback"

        txn["category"] = category
        txn["confidence"] = CONFIDENCE[layer]
        counts[layer] += 1
        if layer in ("agreed", "anthropic", "triqai_low") and c["merchant"]:
            learned[c["merchant"]] = category

    write_merchant_cache(learned)

    total = len(txns)
    deterministic = counts["dict"] + counts["cache"] + counts["keyword"]
    logger.info(
        "Categorize %d rows | dict %d | cache %d | keyword %d | triqai %d | "
        "agreed %d | anthropic %d | triqai_low %d | other %d | "
        "layers 1+2+kw = %.0f%%",
        total, counts["dict"], counts["cache"], counts["keyword"], counts["triqai"],
        counts["agreed"], counts["anthropic"], counts["triqai_low"], counts["fallback"],
        100 * deterministic / total,
    )
    if 100 * deterministic / total < 70:
        logger.warning(
            "Layers 1+2+keyword cover only %.0f%% (<70%%). The dictionary is too "
            "small -- rebuild it with scripts/build_merchant_dict.py.",
            100 * deterministic / total,
        )
    return txns
