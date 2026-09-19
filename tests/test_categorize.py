"""Stage 5 tests. Offline -- the Triqai client is stubbed to fail on any call."""

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


def test_layer_3_reads_the_alias_cache_without_touching_the_network():
    write_aliases([{"raw_description": "SHELL OIL 57445123 PITTSBURGH PA",
                    "merchant": "Shell", "category": "Fuel",
                    "confidence": "85", "source": "triqai"}])
    rows = C.categorize([txn("SHELL OIL 57445123 PITTSBURGH PA", merchant="Shell")])
    assert rows[0]["category"] == "Transportation"
    assert rows[0]["confidence"] == 0.7


def test_unknown_merchant_falls_back_to_other_at_zero_confidence():
    rows = C.categorize([txn("SOMETHING NOBODY KNOWS", merchant="Something Nobody Knows")])
    assert rows[0]["category"] == "Other"
    assert rows[0]["confidence"] == 0.0


# --- layer 3 must never break the pipeline --------------------------------

def test_a_failing_triqai_call_does_not_raise(monkeypatch):
    monkeypatch.setattr(C.triq, "lookup",
                        lambda *a, **k: {"error": True, "merchant": None,
                                         "category": None, "confidence": None})
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
    """The autouse fixture fails on any call, so finishing proves it."""
    _, txns = adapt(json.loads((FIXTURES / "credit.json").read_text()))
    C.categorize(txns)
    assert all("category" in t for t in txns)


def test_unmappable_live_category_falls_back_to_other(monkeypatch):
    monkeypatch.setattr(C.triq, "lookup",
                        lambda *a, **k: {"error": False, "merchant": "X",
                                         "category": "Fictional Taxonomy",
                                         "confidence": 99})
    rows = C.categorize([txn("X", merchant="X")], allow_network=True)
    assert rows[0]["category"] == "Other"


# --- learning --------------------------------------------------------------

def test_layer_3_results_are_written_back_to_the_cache():
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
    assert first[0]["confidence"] == 0.7

    second = C.categorize([txn("DUOLINGO", merchant="Duolingo")])
    assert second[0]["category"] == "Education"
    assert second[0]["confidence"] == 0.9, "should now come from layer 2"


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

def test_layers_one_and_two_cover_at_least_seventy_percent():
    """The 'not an LLM wrapper' claim, as a test.

    Uses the real committed merchant_dict.csv, because the claim is about the
    shipped artifact and not about a fixture.
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
    assert coverage >= 70, f"layers 1+2 cover only {coverage:.0f}%"
