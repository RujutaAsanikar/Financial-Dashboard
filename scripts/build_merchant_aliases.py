"""Build data/merchant_aliases.csv from Triqai. Run by hand, never by tests.

One call per distinct description, because the API has no batch endpoint.
Costs one credit per description not already cached, so re-running is cheap:
existing rows are skipped, and the Idempotency-Key makes a genuine repeat
replay rather than re-charge.

    export TRIQ_API_KEY=...          # or put it in .env
    python scripts/build_merchant_aliases.py --dry-run     # shows cost, calls nothing
    python scripts/build_merchant_aliases.py

Review the output before committing. Confidence is written to every row, so
raising or lowering normalize's floor later needs no new credits.
"""

import argparse
import csv
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import triq  # noqa: E402
from adapter import adapt  # noqa: E402
from normalize import ALIAS_FIELDS, ALIAS_PATH, normalize  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# Country per fixture: Triqai uses it to disambiguate merchants, and the
# Canadian statement's merchants will not resolve against a US catalogue.
FIXTURE_COUNTRY = {"checking": "CA", "credit": "US"}


def distinct_descriptions() -> list[tuple[str, str]]:
    """Every unique (description, country) across the fixtures, in order."""
    seen: dict[str, str] = {}
    for name, country in FIXTURE_COUNTRY.items():
        path = ROOT / "fixtures" / f"{name}.json"
        if not path.exists():
            continue
        _, txns = adapt(json.loads(path.read_text()))
        for txn in txns:
            raw = (txn["description"] or "").strip()
            if raw:
                seen.setdefault(raw, country)
    return sorted(seen.items())


def load_existing() -> dict[str, dict]:
    if not ALIAS_PATH.exists():
        return {}
    with ALIAS_PATH.open(newline="", encoding="utf-8") as handle:
        return {row["raw_description"]: row for row in csv.DictReader(handle)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="list what would be called and the credit cost, call nothing")
    parser.add_argument("--limit", type=int, default=None,
                        help="stop after this many live calls")
    args = parser.parse_args()

    targets = distinct_descriptions()
    existing = load_existing()
    todo = [(raw, country) for raw, country in targets if raw not in existing]

    print(f"{len(targets)} distinct descriptions, {len(existing)} already cached, "
          f"{len(todo)} to fetch")
    if args.limit:
        todo = todo[:args.limit]
        print(f"limited to {len(todo)}")

    if args.dry_run:
        print(f"\nDRY RUN - would spend {len(todo)} credits. Nothing was called.\n")
        for raw, country in todo:
            print(f"  [{country}] {raw}")
        return 0

    if not todo:
        print("Nothing to do.")
        return 0

    if not triq.api_key():
        print("TRIQ_API_KEY is not set. Put it in .env or export it.", file=sys.stderr)
        return 1

    rows = list(existing.values())
    resolved = no_merchant = errored = 0

    for index, (raw, country) in enumerate(todo, 1):
        result = triq.lookup(raw, country=country)
        merchant = result.get("merchant")
        confidence = result.get("confidence")

        if result.get("error"):
            # A timeout or edge block is not an answer. Writing it down as
            # "no merchant" would permanently poison this description --
            # exactly what happened to GIANT EAGLE #6423, cached as unknown
            # while the same chain resolved at 85 one row earlier.
            errored += 1
            print(f"  [{index}/{len(todo)}] ERR {raw[:46]:46} -> call failed, NOT cached")
            continue

        if merchant:
            resolved += 1
            marker = "ok " if (confidence or 0) >= triq.CONFIDENCE_FLOOR else "LOW"
        else:
            no_merchant += 1
            marker = "-- "

        print(f"  [{index}/{len(todo)}] {marker} {raw[:46]:46} -> "
              f"{merchant or '(no merchant)'} ({confidence}) / {result.get('category')}")
        print(f"              regex fallback would give: {normalize(raw)!r}")

        rows.append({
            "raw_description": raw,
            "merchant": merchant or "",
            "category": result.get("category") or "",
            "confidence": confidence if confidence is not None else "",
            "source": "triqai" if merchant else "triqai-nomerchant",
        })

    ALIAS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with ALIAS_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=ALIAS_FIELDS)
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda r: r["raw_description"]))

    usable = sum(1 for r in rows if r["merchant"]
                 and str(r["confidence"] or 0).replace(".", "").isdigit()
                 and float(r["confidence"] or 0) >= triq.CONFIDENCE_FLOOR)
    print(f"\nWrote {len(rows)} rows to {ALIAS_PATH.relative_to(ROOT)}")
    print(f"  {resolved} resolved, {no_merchant} genuinely had no merchant")
    if errored:
        print(f"  {errored} call(s) FAILED and were not cached -- re-run to retry them")
    print(f"  {usable}/{len(rows)} usable at floor {triq.CONFIDENCE_FLOOR}; "
          f"the rest fall back to regex")
    print("\nReview the CSV before committing. Check every LOW row -- the probe "
          "showed 'Hydro One' at 55 was an invented Ontario utility.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
