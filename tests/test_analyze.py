"""Stage 7 tests. See CLAUDE.md section 5, Stage 7.

CLAUDE.md's seven required cases come first, verbatim. The rest defend the
two things this feature cannot afford to get wrong: inventing a subscription
that does not exist, and misreading a price change.
"""

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from adapter import adapt
from analyze import find_recurring, match_cadence
from normalize import resolve
from transfers import mark_transfers

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def rows(merchant, entries, account="chase-4821", **extra):
    """entries: list of (date, positive charge amount)."""
    return [{"merchant": merchant, "description": merchant, "account_id": account,
             "date": when, "amount": -amount, "is_transfer": False,
             "is_recurring": False, **extra}
            for when, amount in entries]


def only(result):
    assert len(result) == 1, f"expected exactly one subscription, got {result}"
    return result[0]


# --- CLAUDE.md's seven required cases -------------------------------------

def test_netflix_monthly_with_a_price_rise():
    result = only(find_recurring(rows("Netflix", [
        (date(2026, 1, 15), 15.99), (date(2026, 2, 15), 15.99),
        (date(2026, 3, 15), 15.99), (date(2026, 4, 15), 17.99)])))
    assert result["cadence_days"] == 30
    assert result["price_change_pct"] == 12.5
    assert result["kind"] == "fixed"


def test_coffee_is_not_recurring():
    """Irregular gaps: 7, 2 and 13 days. No cadence explains that."""
    assert find_recurring(rows("Coffee Tree Roasters", [
        (date(2026, 3, 2), 4.25), (date(2026, 3, 9), 5.10),
        (date(2026, 3, 11), 3.95), (date(2026, 3, 24), 6.40)])) == []


def test_gym_with_a_skipped_month_is_still_monthly():
    """The case that breaks CLAUDE.md's literal rule.

    Gaps are [31, 59]: median 45, ratio 0.311, matching no cadence. Reading
    the 59 as two monthly periods gives per-period [31.0, 29.5], ratio 0.025.
    """
    result = only(find_recurring(rows("Anytime Fitness", [
        (date(2026, 1, 5), 29.99), (date(2026, 2, 5), 29.99),
        (date(2026, 4, 5), 29.99)])))
    assert result["cadence_days"] == 30
    assert result["price_change_pct"] is None


def test_two_occurrences_are_never_enough():
    assert find_recurring(rows("Netflix", [
        (date(2026, 1, 15), 15.99), (date(2026, 2, 15), 15.99)])) == []


def test_empty_input_returns_empty_list():
    assert find_recurring([]) == []


def test_identical_amounts_report_no_price_change():
    result = only(find_recurring(rows("Spotify", [
        (date(2026, 1, 3), 10.99), (date(2026, 2, 3), 10.99),
        (date(2026, 3, 3), 10.99), (date(2026, 4, 3), 10.99)])))
    assert result["price_change_pct"] is None
    assert result["kind"] == "fixed"


def test_electric_bill_is_recurring_but_variable():
    result = only(find_recurring(rows("Duquesne Light", [
        (date(2026, 1, 10), 62.0), (date(2026, 2, 10), 118.0),
        (date(2026, 3, 10), 74.0), (date(2026, 4, 10), 95.0)])))
    assert result["cadence_days"] == 30
    assert result["kind"] == "variable"


# --- cadence matching ------------------------------------------------------

@pytest.mark.parametrize("gaps,expected", [
    ([7, 7, 8, 7], 7),
    ([14, 14, 15], 14),
    ([31, 28, 31], 30),
    ([91, 89, 92], 90),
    ([365, 366], 365),
    ([31, 59], 30),          # one skip
    ([30, 91, 29], 30),      # two consecutive skips
    ([7, 2, 13], None),
    ([3, 40, 9, 120], None),
    ([1, 1, 1], None),       # too frequent to be any cadence
])
def test_cadence_matching(gaps, expected):
    matched = match_cadence(gaps)
    assert (matched[0] if matched else None) == expected


def test_monthly_is_not_read_as_biweekly_with_skips():
    """Every cadence smaller than the true one can explain the data by
    assuming skipped payments. Preferring the fewest skips is what stops
    [31, 28, 31] coming back as 14."""
    assert match_cadence([31, 28, 31])[0] == 30
    assert match_cadence([14, 14, 15])[0] == 14
    assert match_cadence([91, 89, 92])[0] == 90


def test_too_many_skips_is_rejected():
    """A yearly charge is not a monthly subscription with eleven misses."""
    assert match_cadence([365, 365]) [0] == 365
    assert match_cadence([120, 118]) is None


@pytest.mark.parametrize("gaps", [[], [0], [-5, 30], [30, 0]])
def test_degenerate_gaps_match_nothing(gaps):
    assert match_cadence(gaps) is None


# --- what must never become a subscription --------------------------------

def test_transfers_are_excluded():
    entries = [(date(2026, 1, 20), 450.0), (date(2026, 2, 20), 450.0),
               (date(2026, 3, 20), 450.0)]
    assert find_recurring(rows("Web Bill Payment - Visa", entries,
                               is_transfer=True)) == []


def test_income_is_excluded_even_when_perfectly_regular():
    """A fortnightly salary is textbook biweekly. It is not a subscription,
    and an 'annual cost' against it would be nonsense."""
    salary = [{"merchant": "Payroll Deposit - Employer", "description": "Payroll",
               "account_id": "keystone-6789", "date": date(2026, 1, 1) + timedelta(days=14 * i),
               "amount": 1842.55, "is_transfer": False, "is_recurring": False}
              for i in range(6)]
    assert find_recurring(salary) == []


@pytest.mark.parametrize("merchant", [
    "Cheque No", "Cheque", "Atm Withdrawal - First Bank",
    "Atm Withdrawal - Interac", "Web Funds Transfer - To Savings",
])
def test_regular_but_not_subscriptions_are_excluded(merchant):
    """Stage 4 strips the varying cheque number, so three cheques share one
    merchant key -- and three is the detection threshold."""
    entries = [(date(2026, 1, 5), 100.0), (date(2026, 2, 5), 100.0),
               (date(2026, 3, 5), 100.0)]
    assert find_recurring(rows(merchant, entries)) == []


def test_rows_without_a_merchant_are_ignored():
    entries = [(date(2026, 1, 5), 10.0), (date(2026, 2, 5), 10.0), (date(2026, 3, 5), 10.0)]
    assert find_recurring(rows("", entries)) == []
    assert find_recurring(rows(None, entries)) == []


# --- price change ----------------------------------------------------------

def test_small_moves_are_not_reported_as_price_changes():
    """Under 2% is rounding or a tax tweak, not a price rise."""
    result = only(find_recurring(rows("Hulu", [
        (date(2026, 1, 1), 10.00), (date(2026, 2, 1), 10.05),
        (date(2026, 3, 1), 10.10)])))
    assert result["price_change_pct"] is None


def test_a_price_drop_is_reported_as_negative():
    result = only(find_recurring(rows("Hulu", [
        (date(2026, 1, 1), 20.00), (date(2026, 2, 1), 20.00),
        (date(2026, 3, 1), 15.00)])))
    assert result["price_change_pct"] == -25.0


def test_price_change_compares_first_to_last_not_min_to_max():
    """A price that rose and came back down has not changed."""
    result = only(find_recurring(rows("Hulu", [
        (date(2026, 1, 1), 10.00), (date(2026, 2, 1), 30.00),
        (date(2026, 3, 1), 10.00)])))
    assert result["price_change_pct"] is None
    assert result["kind"] == "variable"


# --- output shape ----------------------------------------------------------

def test_result_matches_the_frozen_subscription_model():
    from models import Subscription

    result = only(find_recurring(rows("Netflix", [
        (date(2026, 1, 15), 15.99), (date(2026, 2, 15), 15.99),
        (date(2026, 3, 15), 17.99)])))

    model = Subscription(**{k: v for k, v in result.items() if k != "kind"})
    assert model.merchant == "Netflix"
    assert isinstance(model.cadence_days, int)
    assert set(Subscription.model_fields) <= set(result)


def test_annual_cost_and_next_expected():
    result = only(find_recurring(rows("Netflix", [
        (date(2026, 1, 15), 15.99), (date(2026, 2, 15), 15.99),
        (date(2026, 3, 15), 15.99)])))
    assert result["amount"] == 15.99
    assert result["annual_cost"] == pytest.approx(15.99 * 365 / 30, abs=0.01)
    assert result["first_seen"] == "2026-01-15"
    assert result["next_expected"] == "2026-04-14"   # last date + cadence


def test_next_expected_uses_the_cadence_not_the_inflated_median_gap():
    """With a skipped month the raw median gap is 45, which would predict the
    next charge six weeks out instead of one month."""
    result = only(find_recurring(rows("Anytime Fitness", [
        (date(2026, 1, 5), 29.99), (date(2026, 2, 5), 29.99),
        (date(2026, 4, 5), 29.99)])))
    assert result["next_expected"] == "2026-05-05"


def test_results_are_sorted_by_annual_cost_descending():
    txns = (rows("Cheap", [(date(2026, 1, 1), 5.0), (date(2026, 2, 1), 5.0),
                           (date(2026, 3, 1), 5.0)]) +
            rows("Pricey", [(date(2026, 1, 1), 99.0), (date(2026, 2, 1), 99.0),
                            (date(2026, 3, 1), 99.0)]))
    result = find_recurring(txns)
    assert [r["merchant"] for r in result] == ["Pricey", "Cheap"]


def test_contributing_rows_are_flagged_and_others_are_not():
    subscription = rows("Netflix", [(date(2026, 1, 15), 15.99),
                                    (date(2026, 2, 15), 15.99),
                                    (date(2026, 3, 15), 15.99)])
    noise = rows("Random Shop", [(date(2026, 1, 20), 8.0)])
    find_recurring(subscription + noise)
    assert all(t["is_recurring"] for t in subscription)
    assert not any(t["is_recurring"] for t in noise)


# --- grouping --------------------------------------------------------------

def test_a_subscription_that_moved_cards_is_one_group():
    """CLAUDE.md is explicit: group across accounts, not per account."""
    txns = (rows("Netflix", [(date(2026, 1, 15), 15.99)], account="old-card") +
            rows("Netflix", [(date(2026, 2, 15), 15.99),
                             (date(2026, 3, 15), 15.99)], account="new-card"))
    result = only(find_recurring(txns))
    assert result["account_id"] == "new-card", "should report the most recent account"


def test_different_merchants_do_not_combine():
    txns = (rows("Netflix", [(date(2026, 1, 1), 10.0), (date(2026, 2, 1), 10.0)]) +
            rows("Spotify", [(date(2026, 3, 1), 10.0)]))
    assert find_recurring(txns) == []


# --- robustness ------------------------------------------------------------

@pytest.mark.parametrize("txns", [None, "not a list", [None, "junk", 42], [{}]])
def test_malformed_input_does_not_raise(txns):
    assert find_recurring(txns) == []


def test_rows_with_bad_dates_or_amounts_are_skipped():
    entries = [(date(2026, 1, 5), 10.0), (date(2026, 2, 5), 10.0), (date(2026, 3, 5), 10.0)]
    txns = rows("Netflix", entries)
    txns.append({"merchant": "Netflix", "date": "not a date", "amount": -10.0,
                 "account_id": "x", "is_transfer": False, "description": "x"})
    txns.append({"merchant": "Netflix", "date": date(2026, 4, 5), "amount": None,
                 "account_id": "x", "is_transfer": False, "description": "x"})
    assert only(find_recurring(txns))["cadence_days"] == 30


def test_string_dates_are_accepted():
    txns = [{"merchant": "Netflix", "description": "N", "account_id": "a",
             "date": f"2026-0{m}-15", "amount": -15.99,
             "is_transfer": False, "is_recurring": False} for m in (1, 2, 3)]
    assert only(find_recurring(txns))["cadence_days"] == 30


# --- the real fixtures -----------------------------------------------------

def test_no_false_positives_on_the_fixtures():
    """One month of data. Nothing should reach three occurrences on a real
    cadence, and anything that does gets read by hand below."""
    txns = []
    for name in ("checking", "credit"):
        _, adapted = adapt(json.loads((FIXTURES / f"{name}.json").read_text()))
        txns += adapted
    for txn in txns:
        txn["merchant"] = resolve(txn["description"], allow_network=False)
    mark_transfers(txns)

    for found in find_recurring(txns):
        assert found["cadence_days"] in CADENCES_OK, found
        assert not NOT_SUBSCRIPTION_WORDS.search(found["merchant"]), (
            f"{found['merchant']} is not a subscription")


CADENCES_OK = (7, 14, 30, 90, 365)
import re  # noqa: E402
NOT_SUBSCRIPTION_WORDS = re.compile(r"cheque|atm|transfer|payroll|deposit", re.I)


# --- subscription vs repeated spending ------------------------------------

import analyze as A  # noqa: E402

# Captured at import, before the autouse fixture replaces it. The three tests
# that exercise classify_live's own error handling restore this.
_REAL_CLASSIFY_LIVE = A.classify_live


@pytest.fixture(autouse=True)
def isolated_kinds(tmp_path, monkeypatch):
    """Own kinds cache per test, and a hard ban on reaching Claude.

    Anything that tries a live call fails loudly rather than silently making
    the suite depend on someone's API key.
    """
    monkeypatch.setattr(A, "KINDS_PATH", tmp_path / "recurring_kinds.csv")
    monkeypatch.setattr(A, "_kinds", None)
    monkeypatch.setattr(A, "_live_calls", 0)
    monkeypatch.delenv(A.LIVE_CLASSIFY_ENV, raising=False)
    monkeypatch.setattr(
        A, "classify_live",
        lambda *a, **k: pytest.fail("a test attempted a live Claude call"))
    yield


def write_kinds(pairs):
    import csv
    with A.KINDS_PATH.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(A.KINDS_FIELDS)
        writer.writerows([[m, n, "test"] for m, n in pairs])
    A.load_recurring_kinds(force=True)


@pytest.mark.parametrize("category,expected", [
    ("Subscriptions", A.SUBSCRIPTION),
    ("Utilities", A.BILL),
    ("Housing", A.BILL),
    ("Food & Drink", A.HABITUAL),
    ("Groceries", A.HABITUAL),
    ("Transportation", A.HABITUAL),
    ("Shopping", A.HABITUAL),
])
def test_category_resolves_nature_without_any_model(category, expected):
    """The free layer. Only what it cannot answer costs a credit."""
    assert A.classify_nature("Whatever", category) == expected


@pytest.mark.parametrize("category", [
    "Health", "Entertainment", "Education", "Travel", "Other", None, "",
])
def test_genuinely_ambiguous_categories_are_not_guessed(category):
    assert A.classify_nature("Unknown Merchant", category) == A.UNCLEAR


def test_cache_resolves_what_category_cannot():
    """A gym is Health but is a subscription; insurance is Other but is a bill."""
    write_kinds([("Anytime Fitness", A.SUBSCRIPTION),
                 ("Pre-Auth. Payment - Insurance", A.BILL)])
    assert A.classify_nature("Anytime Fitness", "Health") == A.SUBSCRIPTION
    assert A.classify_nature("Pre-Auth. Payment - Insurance", "Other") == A.BILL


def test_category_wins_over_the_cache():
    """Deterministic beats learned, so a bad cache row cannot move a
    merchant the category already settles."""
    write_kinds([("Netflix", A.HABITUAL)])
    assert A.classify_nature("Netflix", "Subscriptions") == A.SUBSCRIPTION


def test_invalid_cache_rows_are_ignored():
    write_kinds([("A", "not a nature"), ("B", ""), ("", A.SUBSCRIPTION)])
    assert A.classify_nature("A", None) == A.UNCLEAR
    assert A.classify_nature("B", None) == A.UNCLEAR


def test_missing_cache_file_is_not_an_error():
    assert not A.KINDS_PATH.exists()
    assert A.classify_nature("Anything", None) == A.UNCLEAR


@pytest.mark.parametrize("nature,goes_to", [
    (A.SUBSCRIPTION, "subscriptions"),
    (A.BILL, "subscriptions"),
    (A.HABITUAL, "repeated"),
    (A.UNCLEAR, "repeated"),
])
def test_split_routes_each_nature(nature, goes_to):
    row = {"merchant": "X", "nature": nature, "annual_cost": 10.0}
    subscriptions, repeated = A.split_recurring([row])
    assert (len(subscriptions), len(repeated)) == ((1, 0) if goes_to == "subscriptions" else (0, 1))


def test_unclear_never_becomes_a_subscription():
    """The asymmetry that decides the default: a fabricated subscription is
    on screen and wrong; an under-labelled habit is merely unremarkable."""
    rows = [{"merchant": "Mystery", "nature": A.UNCLEAR, "annual_cost": 500.0}]
    subscriptions, repeated = A.split_recurring(rows)
    assert subscriptions == []
    assert repeated[0]["merchant"] == "Mystery"


def test_both_sides_stay_sorted_by_annual_cost():
    rows = [{"merchant": "A", "nature": A.SUBSCRIPTION, "annual_cost": 10.0},
            {"merchant": "B", "nature": A.SUBSCRIPTION, "annual_cost": 99.0},
            {"merchant": "C", "nature": A.HABITUAL, "annual_cost": 5.0},
            {"merchant": "D", "nature": A.HABITUAL, "annual_cost": 50.0}]
    subscriptions, repeated = A.split_recurring(rows)
    assert [r["merchant"] for r in subscriptions] == ["B", "A"]
    assert [r["merchant"] for r in repeated] == ["D", "C"]


def test_split_handles_empty_and_none():
    assert A.split_recurring([]) == ([], [])
    assert A.split_recurring(None) == ([], [])


def test_find_recurring_emits_the_new_fields():
    entries = [(date(2026, 1, 15), 15.99), (date(2026, 2, 15), 15.99),
               (date(2026, 3, 15), 15.99)]
    result = only(find_recurring(rows("Netflix", entries, category="Subscriptions")))
    assert result["nature"] == A.SUBSCRIPTION
    assert result["occurrences"] == 3
    assert result["last_seen"] == "2026-03-15"


def test_a_regular_coffee_habit_is_not_a_subscription():
    """Weekly, fixed amount, perfectly regular -- and still not a subscription."""
    entries = [(date(2026, 1, 5) + timedelta(days=7 * i), 5.25) for i in range(8)]
    found = only(find_recurring(rows("Coffee Tree Roasters", entries,
                                     category="Food & Drink")))
    assert found["nature"] == A.HABITUAL

    subscriptions, repeated = A.split_recurring([found])
    assert subscriptions == []
    assert repeated[0]["occurrences"] == 8


def test_results_validate_against_the_frozen_models():
    from models import RepeatedSpending, Subscription

    subscription = only(find_recurring(rows("Netflix", [
        (date(2026, 1, 15), 15.99), (date(2026, 2, 15), 15.99),
        (date(2026, 3, 15), 15.99)], category="Subscriptions")))
    habit = only(find_recurring(rows("Giant Eagle", [
        (date(2026, 1, 3), 82.0), (date(2026, 1, 10), 74.0),
        (date(2026, 1, 17), 91.0)], category="Groceries")))

    Subscription(**{k: v for k, v in subscription.items()
                    if k in Subscription.model_fields})
    RepeatedSpending(**{k: v for k, v in habit.items()
                        if k in RepeatedSpending.model_fields})


# --- the live fallback ----------------------------------------------------

def stub_live(monkeypatch, answers, calls=None):
    """Replace the live call with a recorder returning `answers`."""
    def fake(merchants):
        if calls is not None:
            calls.append(list(merchants))
        return {m: answers[m] for m in merchants if m in answers}
    monkeypatch.setattr(A, "classify_live", fake)


def test_live_is_off_by_default():
    """The autouse fixture fails on any live call, so reaching the end proves it."""
    assert A.classify_natures([("Mystery Merchant", "Other")]) == \
        {"Mystery Merchant": A.UNCLEAR}
    assert A.live_classifications_used() == 0


def test_live_only_asked_about_what_nothing_else_resolved(monkeypatch):
    """Category and cache come first. Only the true residue costs anything."""
    write_kinds([("Known Gym", A.SUBSCRIPTION)])
    calls = []
    stub_live(monkeypatch, {"Mystery": A.SUBSCRIPTION}, calls)

    result = A.classify_natures([
        ("Netflix", "Subscriptions"),      # category settles it
        ("Coffee Shop", "Food & Drink"),   # category settles it
        ("Known Gym", "Health"),           # cache settles it
        ("Mystery", "Other"),              # nobody settles it
    ], allow_network=True)

    assert calls == [["Mystery"]], "asked about more than the residue"
    assert result["Netflix"] == A.SUBSCRIPTION
    assert result["Known Gym"] == A.SUBSCRIPTION
    assert result["Mystery"] == A.SUBSCRIPTION


def test_everything_resolved_means_no_call_at_all(monkeypatch):
    stub_live(monkeypatch, {}, calls := [])
    A.classify_natures([("Netflix", "Subscriptions")], allow_network=True)
    assert calls == []


def test_one_batched_call_not_one_per_merchant(monkeypatch):
    """N sequential round-trips on an upload request would be a timeout."""
    calls = []
    stub_live(monkeypatch, {f"M{i}": A.HABITUAL for i in range(12)}, calls)

    A.classify_natures([(f"M{i}", "Other") for i in range(12)], allow_network=True)

    assert len(calls) == 1
    assert len(calls[0]) == 12


def test_live_answers_are_cached_so_the_next_run_is_free(monkeypatch):
    calls = []
    stub_live(monkeypatch, {"Mystery": A.BILL}, calls)

    A.classify_natures([("Mystery", "Other")], allow_network=True)
    assert len(calls) == 1

    monkeypatch.setattr(A, "_live_calls", 0)
    again = A.classify_natures([("Mystery", "Other")], allow_network=True)
    assert len(calls) == 1, "second run should hit the cache, not the network"
    assert again["Mystery"] == A.BILL


def test_budget_caps_the_batch(monkeypatch):
    monkeypatch.setattr(A, "MAX_LIVE_CLASSIFICATIONS", 3)
    calls = []
    stub_live(monkeypatch, {}, calls)

    A.classify_natures([(f"M{i}", "Other") for i in range(10)], allow_network=True)

    assert len(calls[0]) == 3, "budget not enforced"


def test_exhausted_budget_degrades_to_unclear(monkeypatch):
    monkeypatch.setattr(A, "MAX_LIVE_CLASSIFICATIONS", 0)
    stub_live(monkeypatch, {"Mystery": A.SUBSCRIPTION}, calls := [])

    result = A.classify_natures([("Mystery", "Other")], allow_network=True)

    assert calls == []
    assert result["Mystery"] == A.UNCLEAR


@pytest.mark.parametrize("failure", [
    lambda *a, **k: {},                                       # returned nothing
    lambda *a, **k: {"Someone Else": A.SUBSCRIPTION},         # answered the wrong merchant
])
def test_a_useless_live_answer_leaves_the_merchant_unclear(monkeypatch, failure):
    monkeypatch.setattr(A, "classify_live", failure)
    result = A.classify_natures([("Mystery", "Other")], allow_network=True)
    assert result["Mystery"] == A.UNCLEAR


def test_classify_live_never_raises(monkeypatch):
    """No key, no network, a timeout, a malformed response -- all the same."""
    import builtins
    real_import = builtins.__import__

    def no_anthropic(name, *args, **kwargs):
        if name == "anthropic":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_anthropic)
    monkeypatch.setattr(A, "classify_live", _REAL_CLASSIFY_LIVE)
    assert A.classify_live(["Mystery"]) == {}


def test_classify_live_survives_an_exploding_client(monkeypatch):
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(A, "classify_live", _REAL_CLASSIFY_LIVE)
    assert A.classify_live(["Mystery"]) == {}


def test_empty_batch_short_circuits(monkeypatch):
    monkeypatch.setattr(A, "classify_live", _REAL_CLASSIFY_LIVE)
    assert A.classify_live([]) == {}


def test_find_recurring_passes_the_flag_through(monkeypatch):
    calls = []
    stub_live(monkeypatch, {"Anytime Fitness": A.SUBSCRIPTION}, calls)
    entries = [(date(2026, 1, 5), 29.99), (date(2026, 2, 5), 29.99),
               (date(2026, 3, 5), 29.99)]

    found = only(find_recurring(rows("Anytime Fitness", entries, category="Health"),
                                allow_network=True))

    assert calls == [["Anytime Fitness"]]
    assert found["nature"] == A.SUBSCRIPTION
    subscriptions, _ = A.split_recurring([found])
    assert len(subscriptions) == 1


def test_find_recurring_is_offline_by_default():
    entries = [(date(2026, 1, 5), 29.99), (date(2026, 2, 5), 29.99),
               (date(2026, 3, 5), 29.99)]
    found = only(find_recurring(rows("Anytime Fitness", entries, category="Health")))
    assert found["nature"] == A.UNCLEAR


def test_internal_category_field_is_not_leaked():
    """_category is scaffolding for the batch pass; it must not reach the API."""
    entries = [(date(2026, 1, 5), 10.0), (date(2026, 2, 5), 10.0),
               (date(2026, 3, 5), 10.0)]
    found = only(find_recurring(rows("Netflix", entries, category="Subscriptions")))
    assert "_category" not in found


def test_dashboard_response_still_validates_without_the_new_keys():
    """The freeze permits adding a field, not breaking one. A payload built
    against the original Stage 0 contract must still validate."""
    from models import DashboardResponse

    payload = {
        "accounts": [], "summary": {"total_spent": 0.0, "total_income": 0.0,
                                    "net": 0.0, "transaction_count": 0,
                                    "period_start": "2026-01-01",
                                    "period_end": "2026-01-31"},
        "by_category": [], "spending_over_time": [], "subscriptions": [],
        "subscription_totals": {"count": 0, "annual_cost": 0.0, "price_increases": 0},
        "payoff": [], "transfers_excluded": {"count": 0, "total": 0.0},
        "extraction": {"reconciled": True, "delta": 0.0, "rows_needing_review": 0},
    }
    response = DashboardResponse(**payload)
    assert response.repeated_spending == []
    assert response.repeated_spending_totals.count == 0
