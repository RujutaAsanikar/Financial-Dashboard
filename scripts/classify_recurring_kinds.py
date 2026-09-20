"""Label merchants subscription / bill / habitual. Run by hand, never by tests.

Stage 7 proves a merchant recurs on a regular cadence. It cannot tell whether
the customer agreed to be billed or simply has a routine -- Netflix monthly
and Starbucks weekly are statistically identical. Stage 5's category settles
most of them for free (see NATURE_BY_CATEGORY in analyze.py); this script
handles only the residue that category genuinely cannot answer: a gym is
Health but IS a subscription, insurance is Other but IS a bill.

One call for the whole batch. Cost is per DISTINCT MERCHANT, not per
transaction, and the answer caches forever -- what Netflix is does not change.
A typical run is a few dozen merchants for roughly one cent.

    export ANTHROPIC_API_KEY=...
    python scripts/classify_recurring_kinds.py --dry-run   # free, shows the batch
    python scripts/classify_recurring_kinds.py

Review data/recurring_kinds.csv before committing. Anything the model returns
as "unclear" is left out, which routes it to repeated spending -- the safe
side, since a fabricated subscription is on screen and wrong.
"""

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pydantic import BaseModel  # noqa: E402

import analyze as A  # noqa: E402
from categorize import load_merchant_dict  # noqa: E402

MODEL = "claude-opus-4-8"

# Categories that can plausibly recur AND that NATURE_BY_CATEGORY does not
# already settle. Fees, income and transfers are excluded from recurring
# detection upstream, so asking about them would spend credits for nothing.
AMBIGUOUS_CATEGORIES = ("Health", "Entertainment", "Education", "Travel", "Other")

SYSTEM = """\
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


class MerchantKind(BaseModel):
    merchant: str
    nature: str


class Classification(BaseModel):
    merchants: list[MerchantKind]


def candidates() -> list[tuple[str, str]]:
    """(merchant, category) pairs that category alone cannot settle."""
    return sorted(
        (merchant, category)
        for merchant, category in load_merchant_dict().items()
        if category in AMBIGUOUS_CATEGORIES
        and not A.NOT_A_SUBSCRIPTION.search(merchant)
    )


def load_existing() -> dict[str, dict]:
    if not A.KINDS_PATH.exists():
        return {}
    with A.KINDS_PATH.open(newline="", encoding="utf-8") as handle:
        return {row["merchant"]: row for row in csv.DictReader(handle)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="show the batch and the prompt cost, call nothing")
    parser.add_argument("--merchants", help="comma-separated list, instead of deriving")
    args = parser.parse_args()

    if args.merchants:
        batch = [(m.strip(), "?") for m in args.merchants.split(",") if m.strip()]
    else:
        batch = candidates()

    existing = load_existing()
    todo = [(m, c) for m, c in batch if m not in existing]

    print(f"{len(batch)} ambiguous merchant(s), {len(existing)} already labelled, "
          f"{len(todo)} to classify")
    for merchant, category in todo:
        print(f"  [{category}] {merchant}")

    if args.dry_run:
        print("\nDRY RUN - one call would be made. Nothing was sent.")
        return 0
    if not todo:
        print("Nothing to do.")
        return 0

    try:
        import anthropic
    except ImportError:
        print("The anthropic package is not installed:  pip install anthropic",
              file=sys.stderr)
        return 1

    client = anthropic.Anthropic()
    try:
        response = client.messages.parse(
            model=MODEL,
            max_tokens=4096,
            # A short per-merchant judgement, not a reasoning problem.
            output_config={"effort": "low"},
            system=SYSTEM,
            messages=[{"role": "user",
                       "content": "Merchants:\n" + "\n".join(m for m, _ in todo)}],
            output_format=Classification,
        )
    except Exception as exc:
        print(f"Classification failed ({type(exc).__name__}: {exc}). "
              "Nothing was written; every merchant stays in repeated spending.",
              file=sys.stderr)
        return 1

    rows = list(existing.values())
    kept = dropped = 0
    for item in response.parsed_output.merchants:
        nature = (item.nature or "").strip().lower()
        if nature not in A.NATURES or nature == A.UNCLEAR:
            # Not an error -- "unclear" is a valid, useful answer. Leaving it
            # out routes the merchant to repeated spending.
            print(f"  --  {item.merchant:34} unclear -> repeated spending")
            dropped += 1
            continue
        print(f"  ok  {item.merchant:34} {nature}")
        rows.append({"merchant": item.merchant, "nature": nature, "source": "claude"})
        kept += 1

    A.KINDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with A.KINDS_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=A.KINDS_FIELDS)
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda r: r["merchant"]))

    usage = response.usage
    cost = (usage.input_tokens * 5 + usage.output_tokens * 25) / 1_000_000
    print(f"\nWrote {len(rows)} row(s) to {A.KINDS_PATH.relative_to(ROOT)}")
    print(f"  {kept} labelled, {dropped} left unclear")
    print(f"  {usage.input_tokens} in / {usage.output_tokens} out  ~${cost:.4f}")
    print("\nReview the CSV before committing. A wrong 'subscription' row puts a "
          "merchant on the table that the user is not actually paying for.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
