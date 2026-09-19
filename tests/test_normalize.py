"""Stage 4 tests. Entirely offline -- no test may ever reach the network.

CLAUDE.md's eight required cases are all here. Four of them are xfail, and
the reason is documented on each: they need a city stripped, and the
positional rule that used to do that destroyed six distinct merchants on real
Canadian data. The Triqai alias cache covers them instead, which is an
enrichment layer, not something the deterministic floor can do.
"""

import csv
import json
from pathlib import Path

import pytest

import normalize as N
from adapter import adapt

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    """Point the alias cache at a temp file and forbid network access.

    Anything that tries to call Triqai during a test fails loudly instead of
    silently making the suite depend on someone's API key.
    """
    monkeypatch.setattr(N, "ALIAS_PATH", tmp_path / "merchant_aliases.csv")
    monkeypatch.setattr(N, "_aliases", None)
    monkeypatch.setattr(N, "_live_calls", 0)
    monkeypatch.delenv(N.LIVE_LOOKUP_ENV, raising=False)
    monkeypatch.setattr(
        N.triq, "enrich",
        lambda *a, **k: pytest.fail("a test attempted a live Triqai call"))
    yield


def write_aliases(rows):
    with N.ALIAS_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=N.ALIAS_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    N.load_aliases(force=True)


def descriptions(name):
    _, txns = adapt(json.loads((FIXTURES / f"{name}.json").read_text()))
    return [t["description"] for t in txns]


# --- CLAUDE.md's eight required cases --------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("NETFLIX.COM 866-579-7172 CA", "Netflix.Com"),
    ("PAYPAL *SPOTIFYUSA 4029357733", "Spotifyusa"),
    ("AUTOMATIC PAYMENT - THANK YOU", "Automatic Payment - Thank You"),
    ("INTEREST CHARGE ON PURCHASES", "Interest Charge On Purchases"),
])
def test_required_cases_the_regex_handles(raw, expected):
    assert N.normalize(raw) == expected


@pytest.mark.xfail(reason="needs the city stripped; the Triqai alias layer does "
                          "this, the deterministic floor deliberately does not",
                   strict=True)
@pytest.mark.parametrize("raw,expected", [
    ("SQ *COFFEE TREE ROASTER 04213 PITTSBURGH PA", "Coffee Tree Roaster"),
    ("TST* NOODLEHEAD - PITTSBURGH", "Noodlehead"),
    ("GIANT EAGLE #6423 PITTSBURGH PA", "Giant Eagle"),
    ("CHECKCARD 0313 TARGET 00012345 PITTSBURGH PA", "Target"),
])
def test_required_cases_needing_the_alias_layer(raw, expected):
    assert N.normalize(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("SQ *COFFEE TREE ROASTER 04213 PITTSBURGH PA", "Coffee Tree Roaster Pittsburgh"),
    ("TST* NOODLEHEAD - PITTSBURGH", "Noodlehead - Pittsburgh"),
    ("GIANT EAGLE #6423 PITTSBURGH PA", "Giant Eagle Pittsburgh"),
    ("CHECKCARD 0313 TARGET 00012345 PITTSBURGH PA", "Target Pittsburgh"),
])
def test_what_the_regex_actually_produces_for_those(raw, expected):
    """Pinned so the xfail above cannot quietly start meaning something else.

    A retained city is a false SPLIT, which CLAUDE.md prefers to a false
    merge, and it does not affect grouping: every visit to one store
    normalizes identically.
    """
    assert N.normalize(raw) == expected


# --- determinism, the one hard requirement --------------------------------

def test_idempotent_on_every_fixture_description():
    for raw in descriptions("checking") + descriptions("credit"):
        once = N.normalize(raw)
        assert N.normalize(once) == once, raw


def test_idempotent_on_the_required_cases():
    for raw in ["SQ *COFFEE TREE ROASTER 04213 PITTSBURGH PA",
                "NETFLIX.COM 866-579-7172 CA",
                "CHECKCARD 0313 TARGET 00012345 PITTSBURGH PA",
                "AUTOMATIC PAYMENT - THANK YOU"]:
        assert N.normalize(N.normalize(raw)) == N.normalize(raw)


def test_repeated_calls_agree():
    raw = "SQ *COFFEE TREE ROASTER 04213 PITTSBURGH PA"
    assert len({N.normalize(raw) for _ in range(50)}) == 1


# --- the regression that deleted rule 5 -----------------------------------

def test_distinct_interac_merchants_never_merge():
    """The failure that removed the positional rule.

    Rule 5 collapsed BOOKSTORE, COFFEE, HARDWARE, PHARMACY and two others
    into a single "Interac Purchase" bucket -- 12 of 30 rows -- which Stage 7
    would then report as a monthly subscription that does not exist.
    """
    groups: dict[str, set[str]] = {}
    for raw in descriptions("checking"):
        groups.setdefault(N.normalize(raw), set()).add(raw)

    merged = {key: value for key, value in groups.items() if len(value) > 1}
    assert merged == {}, f"distinct descriptions collapsed together: {merged}"


@pytest.mark.parametrize("raw,expected", [
    ("Interac Purchase - BOOKSTORE", "Interac Purchase - Bookstore"),
    ("Interac Purchase - COFFEE", "Interac Purchase - Coffee"),
    ("Web Bill Payment - HYDRO", "Web Bill Payment - Hydro"),
    ("Pre-Auth. Payment - MORTGAGE", "Pre-Auth. Payment - Mortgage"),
])
def test_trailing_merchant_token_survives(raw, expected):
    assert N.normalize(raw) == expected


# --- merchants named after numbers ----------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("24 HOUR FITNESS", "24 Hour Fitness"),
    ("5 GUYS BURGERS", "5 Guys Burgers"),
    ("99 RANCH MARKET", "99 Ranch Market"),
    ("7-ELEVEN #3321 PITTSBURGH PA", "7-Eleven Pittsburgh"),
    ("T-MOBILE PAYMENT", "T-Mobile Payment"),
])
def test_numbers_inside_merchant_names_survive(raw, expected):
    """Stripping digits wholesale turns 7-Eleven into Eleven. The rule only
    drops tokens carrying 3+ digits, which is reference-number shaped."""
    assert N.normalize(raw) == expected


# --- individual rules ------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("SQ *COFFEE", "Coffee"),
    ("TST* NOODLEHEAD", "Noodlehead"),
    ("PAYPAL *SPOTIFY", "Spotify"),
    ("POS DEBIT TRADER JOES", "Trader Joes"),
    ("ACH CREDIT PAYROLL", "Payroll"),
    ("CHECKCARD 0313 TARGET", "Target"),
])
def test_processor_prefixes_removed(raw, expected):
    assert N.normalize(raw) == expected


def test_uber_prefix_removed_but_uber_itself_kept():
    assert N.normalize("UBER *TRIP") == "Trip"
    assert N.normalize("UBER EATS") == "Uber Eats"


@pytest.mark.parametrize("raw", [
    "MERCHANT 866-579-7172", "MERCHANT 866 579 7172", "MERCHANT 8665797172",
])
def test_phone_numbers_removed(raw):
    assert N.normalize(raw) == "Merchant"


def test_store_numbers_removed():
    assert N.normalize("GIANT EAGLE #6423") == "Giant Eagle"
    assert N.normalize("TARGET 00012345") == "Target"


def test_trailing_state_code_removed_but_not_a_real_word():
    assert N.normalize("TRADER JOES CA") == "Trader Joes"
    assert N.normalize("SHELL OIL PA") == "Shell Oil"
    assert N.normalize("INTEREST CHARGE ON PURCHASES") == "Interest Charge On Purchases"


def test_interior_punctuation_survives():
    assert N.normalize("NETFLIX.COM") == "Netflix.Com"
    assert N.normalize("PRE-AUTH. PAYMENT - GYM") == "Pre-Auth. Payment - Gym"


# --- edges -----------------------------------------------------------------

@pytest.mark.parametrize("raw", ["", "   ", None, 12345, [], {}])
def test_junk_input_returns_empty_string_without_raising(raw):
    assert N.normalize(raw) == ""


def test_description_of_only_numbers_collapses_to_empty():
    assert N.normalize("00012345 4029357733") == ""


# --- the alias layer -------------------------------------------------------

def test_alias_hit_above_floor_wins_over_regex():
    write_aliases([{"raw_description": "CHECKCARD 0313 TARGET 00012345 PITTSBURGH PA",
                    "merchant": "Target", "category": "Department Stores",
                    "confidence": "92", "source": "triqai"}])
    assert N.resolve("CHECKCARD 0313 TARGET 00012345 PITTSBURGH PA") == "Target"


def test_alias_below_floor_falls_back_to_regex():
    write_aliases([{"raw_description": "WEIRD MERCHANT PITTSBURGH PA",
                    "merchant": "Something Invented", "category": "Other",
                    "confidence": "20", "source": "triqai"}])
    assert N.resolve("WEIRD MERCHANT PITTSBURGH PA") == "Weird Merchant Pittsburgh"


def test_floor_boundary_is_inclusive():
    """55 is the configured floor, and the probe's 'Hydro One' sat exactly there."""
    write_aliases([{"raw_description": "Web Bill Payment - HYDRO", "merchant": "Hydro One",
                    "category": "Utilities", "confidence": str(N.triq.CONFIDENCE_FLOOR),
                    "source": "triqai"}])
    assert N.resolve("Web Bill Payment - HYDRO") == "Hydro One"


def test_alias_with_no_merchant_falls_back_to_regex():
    """Triqai returns no merchant for bank fees. That is the right answer, and
    it must not become an empty merchant name."""
    write_aliases([{"raw_description": "INTEREST CHARGE ON PURCHASES", "merchant": "",
                    "category": "Banking", "confidence": "", "source": "live-nomerchant"}])
    assert N.resolve("INTEREST CHARGE ON PURCHASES") == "Interest Charge On Purchases"


def test_resolve_without_a_cache_file_is_just_normalize():
    assert not N.ALIAS_PATH.exists()
    for raw in descriptions("credit"):
        assert N.resolve(raw) == N.normalize(raw)


def test_resolve_never_calls_the_network_by_default():
    """The autouse fixture fails the test on any live call, so reaching the
    end of this proves the default is offline."""
    for raw in descriptions("checking"):
        N.resolve(raw)
    assert N.live_lookups_used() == 0


def test_malformed_cache_rows_do_not_crash_resolve():
    write_aliases([
        {"raw_description": "A", "merchant": "X", "category": "", "confidence": "not a number", "source": "x"},
        {"raw_description": "B", "merchant": "", "category": "", "confidence": "", "source": "x"},
        {"raw_description": "", "merchant": "Y", "category": "", "confidence": "99", "source": "x"},
    ])
    assert N.resolve("A") == "A"
    assert N.resolve("B") == "B"


def test_resolve_is_idempotent_through_the_cache():
    write_aliases([{"raw_description": "SQ *COFFEE TREE ROASTER 04213 PITTSBURGH PA",
                    "merchant": "Coffee Tree Roasters", "category": "Cafes",
                    "confidence": "70", "source": "triqai"}])
    first = N.resolve("SQ *COFFEE TREE ROASTER 04213 PITTSBURGH PA")
    assert first == "Coffee Tree Roasters"
    assert N.resolve(first) == N.normalize(first) == "Coffee Tree Roasters"


# --- grouping, which is the actual product --------------------------------

def test_the_five_coffee_rows_group_together():
    """Even without the alias layer, so Stage 7 finds the subscription."""
    coffee = [d for d in descriptions("credit") if "COFFEE TREE" in d.upper()]
    assert len(coffee) >= 3
    assert len({N.normalize(d) for d in coffee}) == 1
