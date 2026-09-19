"""Build data/merchant_dict.csv, the layer-1 lookup for Stage 5.

Two sources, no network:

  1. data/merchant_aliases.csv -- Triqai's category for each description,
     translated into CLAUDE.md's 15 via categorize.TRIQ_CATEGORY_MAP.
  2. Keyword rules over the raw description, for the rows Triqai returned
     "Uncategorized" for. 13 of 44 fixture rows landed there, and most are
     trivially readable: "Web Bill Payment - INTERNET" is a utility bill,
     "Payroll Deposit - EMPLOYER" is income. Triqai cannot know that because
     they are generic nouns with no merchant behind them -- which is exactly
     the case a keyword table handles better than any model.

Keys are the NORMALIZED MERCHANT, matching what categorize() looks up, so
layer 1 hits on the same string Stage 4 produced.

    python scripts/build_merchant_dict.py
"""

import csv
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import normalize as N  # noqa: E402
from categorize import CATEGORIES, map_triq_category  # noqa: E402

ALIASES = ROOT / "data" / "merchant_aliases.csv"
OUTPUT = ROOT / "data" / "merchant_dict.csv"

# Applied in order to the raw description; first match wins. Deliberately
# narrow -- a keyword that could belong to two categories is left out rather
# than guessed, because "Other" is honest and a wrong category is not.
KEYWORD_RULES: tuple[tuple[str, str], ...] = (
    (r"payroll|salary|direct deposit", "Income"),
    (r"interest charge|overdraft|annual membership fee|\bfees?\b|nsf|service charge",
     "Fees & Interest"),
    (r"funds transfer|transfer to|\bxfer\b|e-?transfer", "Transfer"),
    (r"atm withdrawal|cash withdrawal", "Transfer"),
    (r"automatic payment|payment - thank you|bill payment - visa|"
     r"bill payment - mastercard|bill payment - amex", "Transfer"),
    (r"mortgage|\brent\b|property tax|condo fee", "Housing"),
    (r"hydro|electric|\bgas bill\b|water bill|internet|\bmobile\b|"
     r"\bcable\b|telecom|wireless", "Utilities"),
    (r"pharmacy|drug ?mart|dental|clinic|medical|optical", "Health"),
    (r"\bgym\b|fitness|yoga", "Health"),
    (r"supermarket|grocer|market\b", "Groceries"),
    (r"restaurant|coffee|cafe|café|bistro|noodle|pizza|bakery", "Food & Drink"),
    (r"gas bar|petro|fuel|shell|esso|chevron", "Transportation"),
    (r"transit|parking|railway|\bbus\b|airline|airport", "Transportation"),
    (r"bookstore|hardware|electronics|clothing|department", "Shopping"),
    (r"tuition|university|college|course", "Education"),
    (r"insurance", "Other"),  # health vs home is unknowable here; do not guess
)

_COMPILED = tuple((re.compile(p, re.I), c) for p, c in KEYWORD_RULES)


def from_keywords(description: str) -> str | None:
    for pattern, category in _COMPILED:
        if pattern.search(description or ""):
            return None if category == "Other" else category
    return None


def main() -> int:
    if not ALIASES.exists():
        print(f"{ALIASES.relative_to(ROOT)} not found. "
              "Run scripts/build_merchant_aliases.py first.", file=sys.stderr)
        return 1

    rows = list(csv.DictReader(ALIASES.open(newline="", encoding="utf-8")))
    entries: dict[str, tuple[str, str]] = {}   # merchant -> (category, source)
    conflicts: list[str] = []

    for row in rows:
        raw = (row.get("raw_description") or "").strip()
        if not raw:
            continue
        category = map_triq_category(row.get("category"))
        source = "triqai"
        if category is None:
            category = from_keywords(raw)
            source = "keyword"
        if category is None:
            continue

        # Key on BOTH spellings of the merchant. resolve() returns the Triqai
        # brand name ("Target") when the alias cache is present and the regex
        # form ("Target Pittsburgh") when it is not. Emitting only the first
        # makes this dictionary silently depend on a file it does not own --
        # delete merchant_aliases.csv and layer 1 drops from 93% to 58%.
        keys = {N.resolve(raw, allow_network=False), N.normalize(raw)}
        for merchant in filter(None, keys):
            existing = entries.get(merchant)
            if existing and existing[0] != category:
                conflicts.append(f"{merchant}: {existing[0]} ({existing[1]}) "
                                 f"vs {category} ({source}) from {raw!r}")
                continue
            entries[merchant] = (category, source)

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["merchant", "category", "source"])
        for merchant in sorted(entries):
            writer.writerow([merchant, *entries[merchant]])

    by_source = Counter(source for _, source in entries.values())
    by_category = Counter(category for category, _ in entries.values())

    print(f"Wrote {len(entries)} merchants to {OUTPUT.relative_to(ROOT)}")
    print(f"  sources: " + ", ".join(f"{s} {n}" for s, n in by_source.most_common()))
    print("  categories:")
    for category, n in by_category.most_common():
        print(f"    {n:3}  {category}")

    unknown = set(by_category) - set(CATEGORIES)
    if unknown:
        print(f"\n  INVALID categories produced: {unknown}", file=sys.stderr)
        return 1
    if conflicts:
        print(f"\n  {len(conflicts)} merchant(s) got conflicting categories:")
        for line in conflicts:
            print(f"    {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
