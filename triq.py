"""Thin Triqai client. Shared by normalize.py and scripts/build_merchant_aliases.py.

Stdlib only -- no new dependency while this is still being evaluated.

Nothing here is on the critical path by default. normalize.py only calls into
this when live lookup is explicitly enabled, and every failure mode returns
None so the caller can fall back to deterministic regex.
"""

import hashlib
import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

ENDPOINT = "https://api.triqai.com/v1/transactions/enrich"
TIMEOUT_SECONDS = 8

# Merchant answers below this confidence are ignored in favour of the regex.
# 55 was chosen deliberately: it accepts "Hydro One" (55), which the probe
# showed to be an invented Ontario utility inferred from the bare word
# "hydro". Confidence is stored alongside every cached row so a later review
# can raise this without re-spending credits.
CONFIDENCE_FLOOR = int(os.getenv("TRIQ_CONFIDENCE_FLOOR", "55"))


def api_key() -> str | None:
    """Read the key from the environment, falling back to .env.

    Never logged, never written to any artifact this module produces.
    """
    key = os.getenv("TRIQ_API_KEY")
    if key:
        return key
    env_file = Path(__file__).resolve().parent / ".env"
    if not env_file.exists():
        return None
    for line in env_file.read_text().splitlines():
        name, _, value = line.strip().partition("=")
        if name.strip() == "TRIQ_API_KEY":
            return value.strip().strip("\"'") or None
    return None


def enrich(title: str, country: str = "US", txn_type: str = "expense") -> dict | None:
    """One enrichment call. Returns the parsed payload, or None on any failure.

    The Idempotency-Key is a hash of the description, so repeating a lookup
    replays the stored response rather than spending another credit -- which
    matters on a 100-credit monthly tier.
    """
    key = api_key()
    if not key:
        logger.warning("TRIQ_API_KEY is not set; skipping live lookup")
        return None

    request = urllib.request.Request(
        ENDPOINT,
        data=json.dumps({"title": title, "country": country, "type": txn_type}).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            # urllib's default UA is refused at Cloudflare's edge with
            # error 1010 before the request ever reaches Triqai.
            "User-Agent": "dashboard-backend/1.0",
            "X-API-Key": key,
            "Idempotency-Key": "dash-" + hashlib.sha256(title.encode()).hexdigest()[:32],
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:200]
        if "error code: 10" in body:
            logger.warning("Triqai edge-blocked the request (Cloudflare), not an API error")
        else:
            logger.warning("Triqai HTTP %s for %r: %s", exc.code, title[:40], body)
    except Exception as exc:
        logger.warning("Triqai call failed for %r: %s: %s", title[:40], type(exc).__name__, exc)
    return None


def extract(payload: dict | None) -> dict:
    """Pull the fields we use out of a response. Total on malformed input."""
    if not isinstance(payload, dict):
        return {"merchant": None, "category": None, "confidence": None, "intermediary": None}

    data = payload.get("data") or {}
    entities = data.get("entities") or []
    if not isinstance(entities, list):
        entities = []

    def pick(kind: str, role: str | None = None) -> dict:
        for entity in entities:
            if not isinstance(entity, dict) or entity.get("type") != kind:
                continue
            if role is None or entity.get("role") == role:
                return entity
        return {}

    merchant = pick("merchant", "primary") or pick("merchant")
    category = ((data.get("transaction") or {}).get("category") or {}).get("primary") or {}

    name = (merchant.get("data") or {}).get("name")
    confidence = (merchant.get("confidence") or {}).get("value")

    return {
        "merchant": name or None,
        "category": category.get("name") or None,
        "confidence": confidence,
        "intermediary": ((pick("intermediary").get("data")) or {}).get("name"),
    }


def lookup(title: str, country: str = "US") -> dict:
    """enrich() + extract() in one call. Always returns the dict shape."""
    return extract(enrich(title, country=country))
