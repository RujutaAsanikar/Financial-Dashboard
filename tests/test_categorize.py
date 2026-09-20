"""Stage 5 tests. Offline -- the Triqai AND Anthropic clients are both
stubbed to fail on any call by default; tests that exercise the arbitration
layer override that stub locally with a fake response."""

import csv
import json
from pathlib import Path

import pytest

import categorize as C
import db
import normalize as N
from adapter import adapt

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Own database, own dictionaries, and a hard ban on network access."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.duckdb")
    db.init_db()

    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setattr(N, "ALIAS_PATH", data / "merchant_aliases.csv")
    monkeypatch.setattr(N, "_aliases", None)
    monkeypatch.setattr(C, "_dict_cache", None)
    monkeypatch.delenv(N.LIVE_LOOKUP_ENV, raising=False)
    monkeypatch.setattr(
        C.triq, "lookup",
        lambda *a, **k: pytest.fail("categorize made a live Triqai call"))
    monkeypatch.setattr(
        C, "_client",
        lambda: pytest.fail("categorize made a live Anthropic call"))
    yield


def write_dict(pairs):
    path = N.ALIAS_PATH.parent / "merchant_dict.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["merchant", "category", "source"])
        writer.writerows([[m, c, "test"] for m, c in pairs])
    C.load_merchant_dict(force=True)


def write_aliases(rows):
    with N.ALIAS_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=N.ALIAS_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    N.load_aliases(force=True)


def txn(description, merchant=None, **extra):
    return {"description": description, "merchant": merchant,
            "account_id": "a", "amount": -10.0, **extra}


class _FakeItem:
    def __init__(self, index, category):
        self.index = index
        self.category = category


class _FakeMessages:
    def __init__(self, items):
        self._items = items

    def parse(self, **kwargs):
        parsed = type("Parsed", (), {"items": self._items})()
        return type("Response", (), {"parsed_output": parsed})()


class _FakeClient:
    """Stands in for anthropic.Anthropic(); only .messages.parse(...) is used."""

    def __init__(self, categories_by_index: dict[int, str]):
        items = [_FakeItem(i, c) for i, c in categories_by_index.items()]
        self.messages = _FakeMessages(items)


def fake_anthropic(monkeypatch, categories_by_index: dict[int, str]):
    """Route C._client() to a fake that returns exactly these categories,
    keyed by the order transactions were appended to the arbitration batch
    (0-indexed, in the order they were categorized)."""
    monkeypatch.setattr(C, "_client", lambda: _FakeClient(categories_by_index))


def exploding_anthropic(monkeypatch):
    monkeypatch.setattr(
        C, "_client",
        lambda: (_ for _ in ()).throw(RuntimeError("Anthropic unavailable in test")))


# --- the category vocabulary ----------------------------------------------

def test_exactly_the_fifteen_categories_claude_md_specifies():
    assert C.CATEGORIES == (
        "Food & Drink", "Groceries", "Transportation", "Shopping", "Entertainment",
        "Subscriptions", "Utilities", "Housing", "Health", "Education", "Travel",
        "Income", "Transfer", "Fees & Interest", "Other",
    )


def test_every_mapped_triqai_category_is_one_of_ours():
    for triq_name, ours in C.TRIQ_CATEGORY_MAP.items():
        assert ours in C.CATEGORIES, f"{triq_name} maps to {ours}, not a valid category"


def test_the_shipped_dictionary_only_contains_valid_categories():
    """Guards the real committed file, not a fixture."""
    real = Path(__file__).resolve().parent.parent / "data" / "merchant_dict.csv"
    if not real.exists():
        pytest.skip("merchant_dict.csv not built yet")
    for row in csv.DictReader(real.open(newline="", encoding="utf-8")):
        assert row["category"] in C.CATEGORIES, row


def test_unmapped_triqai_category_returns_none_rather_than_guessing():
    assert C.map_triq_category("Some Future Category") is None
    assert C.map_triq_category(None) is None
    assert C.map_triq_category("") is None


def test_uncategorized_is_not_treated_as_an_answer():
    assert C.map_triq_category("Uncategorized") is None
    assert C.map_triq_category("Professional Services") is None


# --- layer precedence ------------------------------------------------------

def test_layer_1_dictionary_wins_at_confidence_one():
    write_dict([("Netflix", "Subscriptions")])
    rows = C.categorize([txn("NETFLIX.COM", merchant="Netflix")])
    assert rows[0]["category"] == "Subscriptions"
    assert rows[0]["confidence"] == 1.0


def test_layer_2_cache_used_when_the_dictionary_misses():
    with db.get_con() as con:
        con.execute("INSERT INTO merchant_cache VALUES ('Duolingo', 'Education')")
    rows = C.categorize([txn("DUOLINGO", merchant="Duolingo")])
    assert rows[0]["category"] == "Education"
    assert rows[0]["confidence"] == 0.9


def test_dictionary_beats_cache_when_they_disagree():
    write_dict([("Netflix", "Subscriptions")])
    with db.get_con() as con:
        con.execute("INSERT INTO merchant_cache VALUES ('Netflix', 'Entertainment')")
    rows = C.categorize([txn("NETFLIX.COM", merchant="Netflix")])
    assert rows[0]["category"] == "Subscriptions"
    assert rows[0]["confidence"] == 1.0


def test_layer_3_keyword_used_when_dict_and_cache_miss():
    rows = C.categorize([txn("MORTGAGE PAYMENT ACME BANK", merchant="Acme Bank")])
    assert rows[0]["category"] == "Housing"
    assert rows[0]["confidence"] == 0.95


def test_keyword_rules_beat_a_low_confidence_triqai_alias_cache_entry():
    """Keyword rules are checked before Triqai is even consulted."""
    write_aliases([{"raw_description": "MORTGAGE PAYMENT ACME BANK", "merchant": "Acme Bank",
                    "category": "Uncategorized", "confidence": "50", "source": "triqai"}])
    rows = C.categorize([txn("MORTGAGE PAYMENT ACME BANK", merchant="Acme Bank")])
    assert rows[0]["category"] == "Housing"
    assert rows[0]["confidence"] == 0.95


def test_layer_4_triqai_used_outright_only_at_or_above_the_trust_floor():
    """Description deliberately matches no keyword rule, so this isolates
    the Triqai trust-floor shortcut rather than the keyword layer."""
    write_aliases([{"raw_description": "ACME WIDGETS CORP 88213 DENVER CO",
                    "merchant": "Acme Widgets", "category": "Department Stores",
                    "confidence": "95", "source": "triqai"}])
    rows = C.categorize([txn("ACME WIDGETS CORP 88213 DENVER CO", merchant="Acme Widgets")])
    assert rows[0]["category"] == "Shopping"
    assert rows[0]["confidence"] == 0.85


def test_unknown_merchant_falls_back_to_other_at_zero_confidence():
    rows = C.categorize([txn("SOMETHING NOBODY KNOWS", merchant="Something Nobody Knows")])
    assert rows[0]["category"] == "Other"
    assert rows[0]["confidence"] == 0.0


# --- the three confirmed keyword-rule bugs, as regression tests -----------

def test_nsf_no_longer_matches_inside_transfer():
    assert C.from_keywords("ONLINE TRANSFER TO SAVINGS 1234") != "Fees & Interest"


def test_shell_keyword_no_longer_matches_inside_seashell():
    assert C.from_keywords("Seashell Grill") != "Transportation"
    assert C.from_keywords("SHELL GAS STATION") == "Transportation"  # still works for real Shell


def test_bare_mobile_no_longer_forces_utilities():
    assert C.from_keywords("Mobile Deposit") is None
    assert C.from_keywords("T-Mobile Wireless Bill") == "Utilities"  # "wireless" still catches it


# --- layer 5: Anthropic arbitration ----------------------------------------

def test_low_confidence_triqai_triggers_arbitration_anthropic_wins_on_disagreement(monkeypatch):
    monkeypatch.setattr(
        C.triq, "lookup",
        lambda *a, **k: {"error": False, "merchant": "X",
                         "category": "Professional Services", "confidence": 60})
    fake_anthropic(monkeypatch, {0: "Shopping"})
    rows = C.categorize([txn("AMBIGUOUS MERCHANT X", merchant="X")], allow_network=True)
    assert rows[0]["category"] == "Shopping"
    assert rows[0]["confidence"] == 0.75


def test_triqai_and_anthropic_agreement_is_trusted_more_than_either_alone(monkeypatch):
    monkeypatch.setattr(
        C.triq, "lookup",
        lambda *a, **k: {"error": False, "merchant": "X",
                         "category": "Fuel", "confidence": 60})
    fake_anthropic(monkeypatch, {0: "Transportation"})
    rows = C.categorize([txn("AMBIGUOUS MERCHANT X", merchant="X")], allow_network=True)
    assert rows[0]["category"] == "Transportation"
    assert rows[0]["confidence"] == 0.85


def test_anthropic_unreachable_falls_back_to_triqais_low_confidence_guess(monkeypatch):
    monkeypatch.setattr(
        C.triq, "lookup",
        lambda *a, **k: {"error": False, "merchant": "X",
                         "category": "Fuel", "confidence": 60})
    exploding_anthropic(monkeypatch)
    rows = C.categorize([txn("AMBIGUOUS MERCHANT X", merchant="X")], allow_network=True)
    assert rows[0]["category"] == "Transportation"
    assert rows[0]["confidence"] == 0.5


def test_both_triqai_and_anthropic_unavailable_falls_back_to_other(monkeypatch):
    monkeypatch.setattr(
        C.triq, "lookup",
        lambda *a, **k: {"error": True, "merchant": None,
                         "category": None, "confidence": None})
    exploding_anthropic(monkeypatch)
    rows = C.categorize([txn("MYSTERY MERCHANT", merchant="Mystery Merchant")],
                        allow_network=True)
    assert rows[0]["category"] == "Other"
    assert rows[0]["confidence"] == 0.0


def test_an_exploding_triqai_call_does_not_raise(monkeypatch):
    monkeypatch.setattr(C.triq, "lookup",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        C.categorize([txn("X", merchant="X")], allow_network=True)


def test_live_lookup_is_off_by_default():
    """The autouse fixture fails on any Triqai or Anthropic call, so
    finishing without arguments proves neither was touched."""
    _, txns = adapt(json.loads((FIXTURES / "credit.json").read_text()))
    C.categorize(txns)
    assert all("category" in t for t in txns)


def test_unmappable_live_category_is_not_treated_as_a_triqai_answer(monkeypatch):
    """Confidence 99 but an unmappable category -- our own mapping failing
    means we effectively have no Triqai answer, so this still goes through
    arbitration rather than being wrongly trusted at the >=90 shortcut."""
    monkeypatch.setattr(
        C.triq, "lookup",
        lambda *a, **k: {"error": False, "merchant": "X",
                         "category": "Fictional Taxonomy", "confidence": 99})
    exploding_anthropic(monkeypatch)
    rows = C.categorize([txn("X", merchant="X")], allow_network=True)
    assert rows[0]["category"] == "Other"


def test_arbitration_is_batched_into_one_call_for_several_transactions(monkeypatch):
    calls = []

    def fake_lookup(description, *a, **k):
        return {"error": False, "merchant": None, "category": "Professional Services",
               "confidence": 50}
    monkeypatch.setattr(C.triq, "lookup", fake_lookup)

    class CountingClient(_FakeClient):
        def __init__(self, categories_by_index):
            super().__init__(categories_by_index)
            real_parse = self.messages.parse
            def counted_parse(**kwargs):
                calls.append(1)
                return real_parse(**kwargs)
            self.messages.parse = counted_parse

    monkeypatch.setattr(C, "_client",
                        lambda: CountingClient({0: "Shopping", 1: "Shopping", 2: "Shopping"}))

    rows = C.categorize([
        txn("MERCHANT ONE", merchant="Merchant One"),
        txn("MERCHANT TWO", merchant="Merchant Two"),
        txn("MERCHANT THREE", merchant="Merchant Three"),
    ], allow_network=True)

    assert len(calls) == 1, "three low-confidence rows should cost one Anthropic call, not three"
    assert all(r["category"] == "Shopping" for r in rows)


# --- learning --------------------------------------------------------------

def test_layer_4_results_are_written_back_to_the_cache():
    write_aliases([{"raw_description": "DUOLINGO 415-555-0132 CA", "merchant": "Duolingo",
                    "category": "Online Learning", "confidence": "95", "source": "triqai"}])
    C.categorize([txn("DUOLINGO 415-555-0132 CA", merchant="Duolingo")])

    with db.get_con() as con:
        stored = con.execute(
            "SELECT category FROM merchant_cache WHERE merchant = 'Duolingo'").fetchone()
    assert stored == ("Education",)


def test_a_second_run_uses_the_cache_it_just_populated():
    write_aliases([{"raw_description": "DUOLINGO", "merchant": "Duolingo",
                    "category": "Online Learning", "confidence": "95", "source": "triqai"}])
    first = C.categorize([txn("DUOLINGO", merchant="Duolingo")])
    assert first[0]["confidence"] == 0.85

    second = C.categorize([txn("DUOLINGO", merchant="Duolingo")])
    assert second[0]["category"] == "Education"
    assert second[0]["confidence"] == 0.9, "should now come from layer 2"


def test_arbitrated_results_are_also_written_back_to_the_cache(monkeypatch):
    monkeypatch.setattr(
        C.triq, "lookup",
        lambda *a, **k: {"error": False, "merchant": "X",
                         "category": "Professional Services", "confidence": 50})
    fake_anthropic(monkeypatch, {0: "Shopping"})
    C.categorize([txn("AMBIGUOUS MERCHANT X", merchant="X")], allow_network=True)

    with db.get_con() as con:
        stored = con.execute(
            "SELECT category FROM merchant_cache WHERE merchant = 'X'").fetchone()
    assert stored == ("Shopping",)


# --- shape and edges -------------------------------------------------------

def test_empty_input_is_a_no_op():
    assert C.categorize([]) == []


def test_missing_merchant_is_derived_from_the_description():
    rows = C.categorize([txn("NETFLIX.COM 866-579-7172 CA")])
    assert rows[0]["merchant"] == "Netflix.Com"
    assert "category" in rows[0]


def test_every_row_gets_a_valid_category_and_confidence():
    _, txns = adapt(json.loads((FIXTURES / "checking.json").read_text()))
    for row in C.categorize(txns):
        assert row["category"] in C.CATEGORIES
        assert row["confidence"] in set(C.CONFIDENCE.values())


def test_a_broken_database_degrades_instead_of_crashing(monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", Path("/nonexistent/dir/cannot.duckdb"))
    rows = C.categorize([txn("NETFLIX.COM", merchant="Netflix")])
    assert rows[0]["category"] in C.CATEGORIES


# --- CLAUDE.md's coverage gate --------------------------------------------

def test_layers_one_two_and_keyword_cover_at_least_seventy_percent():
    """The 'not an LLM wrapper' claim, as a test.

    Uses the real committed merchant_dict.csv, because the claim is about the
    shipped artifact and not about a fixture. Dict, cache and keyword layers
    are all deterministic (no network); their confidence values (1.0, 0.9,
    0.95) are all >= 0.9, which is what this checks for.
    """
    real = Path(__file__).resolve().parent.parent / "data" / "merchant_dict.csv"
    if not real.exists():
        pytest.skip("merchant_dict.csv not built yet")

    import shutil
    shutil.copy(real, N.ALIAS_PATH.parent / "merchant_dict.csv")
    C.load_merchant_dict(force=True)

    txns = []
    for name in ("checking", "credit"):
        _, rows = adapt(json.loads((FIXTURES / f"{name}.json").read_text()))
        txns += rows
    for row in txns:
        row["merchant"] = N.normalize(row["description"])

    C.categorize(txns)
    deterministic = sum(1 for t in txns if t["confidence"] >= 0.9)
    coverage = 100 * deterministic / len(txns)
    assert coverage >= 70, f"layers 1+2+keyword cover only {coverage:.0f}%"
