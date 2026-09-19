"""Evaluate Triqai on five transaction descriptions. Run by hand, never by tests.

This is a decision aid, not part of the pipeline. It answers one question:
does Triqai handle the two description formats where CLAUDE.md's Stage 4
regex rules fail?

Costs 5 credits on a first run. The Idempotency-Key is derived from the
description, so re-running replays the cached response instead of spending
more -- which matters on a 100-credit monthly free tier.

    export TRIQ_API_KEY=...        # or put it in .env
    python scripts/probe_triqai.py

The key is read from the environment and never printed or written anywhere.
"""

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = "https://api.triqai.com/v1/transactions/enrich"

# Five cases chosen because they decide the question. Two are outright
# failures of the spec's regex rules, two are the cases that destroyed 18 of
# 30 rows on the Canadian fixture, and one is a control the regex handles
# correctly. "expected" is what a human would call the merchant.
CASES = [
    {
        "title": "SQ *COFFEE TREE ROASTER 04213 PITTSBURGH PA",
        "country": "US",
        "expected": "Coffee Tree Roaster",
        "why": "CONTROL - regex already gets this right",
    },
    {
        "title": "CHECKCARD 0313 TARGET 00012345 PITTSBURGH PA",
        "country": "US",
        "expected": "Target",
        "why": "regex FAILS -> 'Target Pittsburgh' (rule 5 too timid)",
    },
    {
        "title": "INTEREST CHARGE ON PURCHASES",
        "country": "US",
        "expected": "Interest Charge On Purchases",
        "why": "regex FAILS -> 'Interest Charge On' (rule 5 ate a real word). "
               "Also: not a merchant at all. Does it invent one?",
    },
    {
        "title": "Interac Purchase - BOOKSTORE",
        "country": "CA",
        "expected": "Bookstore",
        "why": "regex CATASTROPHE - merged 6 distinct merchants into one",
    },
    {
        "title": "Web Bill Payment - HYDRO",
        "country": "CA",
        "expected": "Hydro (utility)",
        "why": "regex CATASTROPHE - merged 4 distinct bills into one",
    },
]


def load_dotenv() -> None:
    """Minimal .env reader so the key never has to live in shell history."""
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def enrich(title: str, country: str, api_key: str) -> dict:
    """One enrichment call. Idempotency-Key is stable per description, so a
    repeat run replays rather than re-charging."""
    body = json.dumps({"title": title, "country": country, "type": "expense"}).encode()
    request = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            # urllib's default "Python-urllib/3.x" is blocked at Cloudflare's
            # edge with error 1010 before the request reaches the API. Any
            # ordinary client string gets through, as curl does in Triqai's
            # own documented example.
            "User-Agent": "dashboard-backend-probe/1.0",
            "X-API-Key": api_key,
            "Idempotency-Key": "probe-" + hashlib.sha256(title.encode()).hexdigest()[:32],
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def summarize(payload: dict) -> dict:
    """Pull the three fields that decide this: merchant name, category, confidence."""
    data = payload.get("data") or {}
    entities = data.get("entities") or []

    merchant = next(
        (e for e in entities if e.get("type") == "merchant" and e.get("role") == "primary"),
        next((e for e in entities if e.get("type") == "merchant"), None),
    )
    intermediary = next((e for e in entities if e.get("type") == "intermediary"), None)
    category = ((data.get("transaction") or {}).get("category") or {}).get("primary") or {}

    return {
        "merchant": (merchant or {}).get("data", {}).get("name"),
        "merchant_confidence": (merchant or {}).get("confidence", {}).get("value"),
        "intermediary": (intermediary or {}).get("data", {}).get("name"),
        "category": category.get("name"),
        "partial": payload.get("partial"),
    }


def main() -> int:
    load_dotenv()
    api_key = os.getenv("TRIQ_API_KEY")
    if not api_key:
        print(
            "TRIQ_API_KEY is not set.\n\n"
            "GitHub Actions secrets cannot be read locally -- they only exist inside\n"
            "a runner. For a local probe, put the key in .env (already gitignored):\n\n"
            "    echo 'TRIQ_API_KEY=<your key>' >> .env\n",
            file=sys.stderr,
        )
        return 1

    print(f"Probing Triqai on {len(CASES)} descriptions "
          f"(re-runs replay via Idempotency-Key, so this costs 5 credits once)\n")

    results = []
    for index, case in enumerate(CASES, 1):
        print(f"[{index}/{len(CASES)}] {case['title']}")
        print(f"        why: {case['why']}")
        try:
            got = summarize(enrich(case["title"], case["country"], api_key))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            print(f"        HTTP {exc.code}: {detail[:300]}")
            # Distinguish "the edge blocked us" from "the API said no", since
            # they need completely different fixes and only one costs credits.
            if "error code: 10" in detail or "cloudflare" in detail.lower():
                print("        ^ Cloudflare edge block, not a Triqai response.")
                print("          The request never reached the API, so no credit was used.")
            elif exc.code in (401, 403):
                print("        ^ Reached the API and was rejected: check the key.")
            elif exc.code == 429:
                print("        ^ Rate limited or out of credits.")
            print()
            results.append({**case, "error": f"HTTP {exc.code}", "body": detail[:500]})
            continue
        except Exception as exc:  # network, timeout, malformed JSON
            print(f"        FAILED: {type(exc).__name__}: {exc}\n")
            results.append({**case, "error": str(exc)})
            continue

        print(f"        expected : {case['expected']}")
        print(f"        merchant : {got['merchant']}  (confidence {got['merchant_confidence']})")
        print(f"        category : {got['category']}")
        if got["intermediary"]:
            print(f"        processor: {got['intermediary']}")
        if got["partial"]:
            print("        NOTE: response flagged partial")
        print()
        results.append({**case, **got})

    out = Path(__file__).resolve().parent.parent / "triqai_probe_results.json"
    out.write_text(json.dumps(results, indent=2))
    print(f"Raw results written to {out.name} (gitignored - delete when done)")

    print("\nWhat to look for:")
    print("  - Cases 2 and 3 are where the regex rules fail. Does Triqai fix them?")
    print("  - Cases 4 and 5 are the Interac format. A wrong answer here is")
    print("    disqualifying: that is where the regex destroyed 18 of 30 rows.")
    print("  - Case 3 is not a merchant. Inventing one would be worse than")
    print("    returning nothing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
