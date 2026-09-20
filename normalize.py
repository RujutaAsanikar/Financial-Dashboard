"""Raw statement descriptions -> stable merchant grouping keys. Stage 4.

Stage 7 groups subscriptions by exact string equality on this field, so the
only hard requirement is that the same input always produces the same output.
Prettiness is secondary; stability is not negotiable.

Two layers:

    resolve()    cache lookup -> optional live Triqai call -> normalize()
    normalize()  pure deterministic regex, no network, no state

normalize() is the floor. It always returns something, it never touches the
network, and it is what the tests exercise. resolve() is the enrichment layer
on top, and every one of its failure modes falls back to normalize().

WHY THERE IS NO POSITIONAL CITY RULE
------------------------------------
CLAUDE.md's rule 5 -- drop the last token if it is ALL CAPS, 4+ characters,
and the string has 3+ tokens -- was measured against real data before being
dropped. On the Canadian fixture it collapsed six distinct merchants into a
single "Interac Purchase" bucket (12 of 30 rows), because in
"Interac Purchase - BOOKSTORE" the merchant IS the last token. Stage 7 would
then report a 12-occurrence monthly subscription that does not exist.

It also contradicted CLAUDE.md's own required cases in both directions at
once: too timid on "CHECKCARD 0313 TARGET 00012345 PITTSBURGH PA"
(-> "Target Pittsburgh", rule never fires, only 2 tokens remain) and too eager
on "INTEREST CHARGE ON PURCHASES" (-> "Interest Charge On", eats a real word).
No threshold fixes both, because position cannot distinguish a city from a
noun. Every rule here keys on evidence instead: a known processor prefix, a
phone-number shape, a long digit run, a trailing two-letter state code.

The cost is that a city can survive into the merchant name -- "Giant Eagle
Pittsburgh" rather than "Giant Eagle". That is a false SPLIT, which CLAUDE.md
explicitly prefers to a false merge, and it does not affect grouping: every
occurrence at one store normalizes identically. The Triqai layer resolves
most of these to the clean brand name anyway.
"""

import csv
import logging
import os
import re
import threading
from pathlib import Path

import triq

logger = logging.getLogger(__name__)

ALIAS_PATH = Path(__file__).resolve().parent / "data" / "merchant_aliases.csv"
ALIAS_FIELDS = ("raw_description", "merchant", "category", "confidence", "source")

# Live lookups are opt-in. Tests never set this, so the suite is offline and
# deterministic by construction rather than by discipline.
LIVE_LOOKUP_ENV = "TRIQ_LIVE_LOOKUP"

# Hard cap per process. A 200-row upload where every row misses the cache
# would otherwise be 200 sequential HTTP calls on the request path -- the API
# has no batch endpoint -- and would exhaust a 100-credit month in one go.
MAX_LIVE_LOOKUPS = int(os.getenv("TRIQ_MAX_LIVE_LOOKUPS", "40"))

_PROCESSOR_PREFIXES = (
    "SQ *", "TST* ", "TST*", "PY *", "PAYPAL *", "SP *", "UBER *",
    "POS DEBIT", "ACH DEBIT", "ACH CREDIT", "DEBIT CARD PURCHASE", "VISA DDA PUR",
)

_CHECKCARD = re.compile(r"^CHECKCARD\s+\d{4}\s*", re.I)
_PHONE = re.compile(r"\b\d{3}[-\s]?\d{3}[-\s]?\d{4}\b")
_HASH_NUMBER = re.compile(r"#\s*\d+")
_LONG_DIGITS = re.compile(r"\b\d{3,}\b")
# Uppercase-only, and anchored to the end. Case-insensitive here would strip
# the "Pa" it produced on a previous pass and break idempotence.
_TRAILING_STATE = re.compile(r"\s+[A-Z]{2}\s*$")
# Order/auth codes: letters and digits mixed in one token. Amazon prints
# "US*2K4LM8" and it changes every order, so without this every purchase
# becomes its own merchant and Stage 7 can never see Amazon as recurring.
# Requires 2+ digits and 5+ characters, which spares "7-Eleven" (one digit)
# and short names like "A1".
_ORDER_CODE = re.compile(r"\b(?=[A-Za-z0-9*]{5,}\b)(?=(?:[^\d\s]*\d){2})[A-Za-z0-9*]+\b")
# Day-of-week suffixes: Lyft prints "RIDE THU", which splits every ride.
_TRAILING_DAY = re.compile(r"\s+(MON|TUE|WED|THU|FRI|SAT|SUN)\s*$", re.I)
_WHITESPACE = re.compile(r"\s+")
_EDGE_PUNCTUATION = " -.,*/|:;#"

_lock = threading.Lock()
_aliases: dict[str, dict] | None = None
_live_calls = 0


def _is_reference_token(token: str) -> bool:
    """True for a token that is all punctuation/digits AND carries 3+ digits.

    The digit threshold is what saves merchants named after numbers:
    "24 HOUR FITNESS", "5 GUYS BURGERS", "99 RANCH MARKET" keep their number,
    while "0313" and "00012345" are dropped as reference noise.
    """
    return not re.search(r"[A-Za-z]", token) and len(re.sub(r"\D", "", token)) >= 3


def normalize(description: str) -> str:
    """Deterministic cleanup. No network, no cache, no configuration.

    Idempotent: normalize(normalize(x)) == normalize(x) for all x.
    """
    if not isinstance(description, str):
        return ""

    text = description.strip()

    # 1. Payment-processor prefixes. These vary between occurrences of the
    #    same merchant, so they are noise for grouping purposes.
    text = _CHECKCARD.sub("", text)
    for prefix in _PROCESSOR_PREFIXES:
        if text.upper().startswith(prefix.upper()):
            text = text[len(prefix):]
            break

    # 2. Phone numbers, before any digit-run rule -- otherwise the digit rule
    #    shreds the phone number and leaves its separators behind.
    text = _PHONE.sub(" ", text)

    # 3. Store and reference numbers.
    text = _HASH_NUMBER.sub(" ", text)
    text = _LONG_DIGITS.sub(" ", text)

    # 4. Trailing two-letter state code, then the per-transaction tokens that
    #    would otherwise split one merchant across its own occurrences.
    text = _TRAILING_STATE.sub(" ", text)
    text = _ORDER_CODE.sub(" ", text)
    text = _TRAILING_DAY.sub(" ", text)

    # 5. Leading and trailing reference tokens. Edges only -- never interior,
    #    and never a token that contains letters.
    tokens = text.split()
    while tokens and _is_reference_token(tokens[0]):
        tokens.pop(0)
    while tokens and _is_reference_token(tokens[-1]):
        tokens.pop()

    # 6. Tidy. Punctuation is stripped only at the ends, so "Netflix.Com" and
    #    "Automatic Payment - Thank You" keep the punctuation inside them.
    text = _WHITESPACE.sub(" ", " ".join(tokens)).strip()
    return text.strip(_EDGE_PUNCTUATION).title()


def load_aliases(force: bool = False) -> dict[str, dict]:
    """Load the reviewed alias cache. Missing file is not an error."""
    global _aliases
    with _lock:
        if _aliases is not None and not force:
            return _aliases
        aliases: dict[str, dict] = {}
        if ALIAS_PATH.exists():
            with ALIAS_PATH.open(newline="", encoding="utf-8") as handle:
                for row in csv.DictReader(handle):
                    raw = (row.get("raw_description") or "").strip()
                    if raw:
                        aliases[raw] = row
        _aliases = aliases
        logger.info("Loaded %d merchant aliases from %s", len(aliases), ALIAS_PATH.name)
        return _aliases


def _accept(row: dict | None) -> str | None:
    """A cached row is usable only with a merchant name at or above the floor."""
    if not row:
        return None
    merchant = (row.get("merchant") or "").strip()
    if not merchant:
        return None
    try:
        confidence = float(row.get("confidence") or 0)
    except (TypeError, ValueError):
        return None
    return merchant if confidence >= triq.CONFIDENCE_FLOOR else None


def _append_alias(raw: str, result: dict, source: str) -> None:
    ALIAS_PATH.parent.mkdir(parents=True, exist_ok=True)
    new_file = not ALIAS_PATH.exists()
    with ALIAS_PATH.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=ALIAS_FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow({
            "raw_description": raw,
            "merchant": result.get("merchant") or "",
            "category": result.get("category") or "",
            "confidence": result.get("confidence") if result.get("confidence") is not None else "",
            "source": source,
        })


def live_lookups_used() -> int:
    return _live_calls


def resolve(description: str, *, country: str = "US",
            allow_network: bool | None = None) -> str:
    """Best available merchant name for a description.

    Order: reviewed cache -> live Triqai call (opt-in) -> normalize().

    allow_network defaults to the TRIQ_LIVE_LOOKUP environment variable, so
    tests and CI stay offline without having to remember to pass anything.
    A live result is written back to the cache, which means the network is
    consulted at most once per distinct description, ever -- after that the
    answer is deterministic and free.

    Any failure at all -- no key, timeout, edge block, rate limit, low
    confidence, no merchant identified -- falls through to normalize().
    """
    global _live_calls

    if not isinstance(description, str) or not description.strip():
        return ""

    raw = description.strip()
    cached = load_aliases().get(raw)
    if cached is not None:
        return _accept(cached) or normalize(description)

    if allow_network is None:
        allow_network = os.getenv(LIVE_LOOKUP_ENV, "").lower() in ("1", "true", "yes")

    if allow_network:
        with _lock:
            budget_left = _live_calls < MAX_LIVE_LOOKUPS
            if budget_left:
                _live_calls += 1
        if not budget_left:
            logger.warning("Live lookup budget of %d exhausted; using regex for %r",
                           MAX_LIVE_LOOKUPS, raw[:40])
        else:
            result = triq.lookup(raw, country=country)
            if result.get("merchant"):
                # Cached even when below the floor: it cost a credit, and a
                # later review can raise or lower the floor without respending.
                _append_alias(raw, result, source="live")
                load_aliases(force=True)
                accepted = _accept({**result, "confidence": result.get("confidence") or 0})
                if accepted:
                    return accepted
            else:
                # Record the miss so we never pay for this description again.
                _append_alias(raw, result, source="live-nomerchant")
                load_aliases(force=True)

    return normalize(description)
