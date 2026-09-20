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
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import normalize as N  # noqa: E402
from categorize import CATEGORIES, from_keywords, map_triq_category  # noqa: E402
# KEYWORD_RULES/from_keywords live in categorize.py now -- it's a runtime
# dependency there (live categorization), this script just reuses the same
# rules at build time so layer 1 and the live keyword layer never drift.

ALIASES = ROOT / "data" / "merchant_aliases.csv"
OUTPUT = ROOT / "data" / "merchant_dict.csv"


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
