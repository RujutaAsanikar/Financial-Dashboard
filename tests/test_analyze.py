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
from analyze import PAYMENTS_PER_YEAR, find_recurring, match_cadence
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
    # 12 payments/year for a monthly (30-day) cadence, not 365/30 (~12.17) --
    # a real monthly bill is charged once a calendar month, not "every 30.0
    # days". See PAYMENTS_PER_YEAR.
    assert result["annual_cost"] == pytest.approx(15.99 * 12, abs=0.01)
    assert result["first_seen"] == "2026-01-15"
    assert result["next_expected"] == "2026-04-14"   # last date + cadence


def test_annual_cost_uses_calendar_payments_per_year_for_every_cadence():
    """All 5 canonical cadences, not just monthly -- weekly/biweekly/
    quarterly were also slightly overstated by 365/cadence_days."""
    cases = [
        (7, 52), (14, 26), (30, 12), (90, 4), (365, 1),
    ]
    for cadence, payments_per_year in cases:
        assert PAYMENTS_PER_YEAR[cadence] == payments_per_year


def test_annual_cost_falls_back_safely_for_an_unmapped_cadence():
    """cadence_days is always one of CADENCES today, but this must degrade
    -- not KeyError and crash the upload -- if that ever stopped being true."""
    assert PAYMENTS_PER_YEAR.get(21) is None  # confirms 21 really is unmapped


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


# --- payoff amortization. Stage 8 -----------------------------------------

import math  # noqa: E402

from analyze import (  # noqa: E402
    calculate_payoff,
    minimum_payment,
    payment_for_months,
    payoff_scenarios,
)

DEMO_BALANCE, DEMO_APR = 3204.18, 24.99


# CLAUDE.md's four required cases -------------------------------------------

def test_the_spec_demo_case_cross_checked_against_the_closed_form():
    """CLAUDE.md asks for this verified against an amortization calculator.

    Done algebraically instead, which is a stronger check than trusting a
    website: n = -log(1 - Br/P) / log(1+r) is derived independently of the
    iterative loop, so agreement rules out a compounding-order bug.

    NOTE: CLAUDE.md asserts 30-34 months and $1,200-$1,700 interest. Both are
    wrong -- the answer is 29 months and $1,079.17, and the Stage 0 mock has
    carried those figures since before this stage existed.
    """
    result = calculate_payoff(DEMO_BALANCE, DEMO_APR, 150.0)

    rate = DEMO_APR / 100 / 12
    closed_form = -math.log(1 - DEMO_BALANCE * rate / 150.0) / math.log(1 + rate)

    assert result["months"] == math.ceil(closed_form) == 29
    assert result["total_interest"] == pytest.approx(1079.17, abs=0.05)


def test_a_payment_below_the_first_months_interest_never_pays_off():
    interest = DEMO_BALANCE * DEMO_APR / 100 / 12
    assert calculate_payoff(DEMO_BALANCE, DEMO_APR, interest - 0.01) is None
    assert calculate_payoff(DEMO_BALANCE, DEMO_APR, interest) is None
    assert calculate_payoff(DEMO_BALANCE, DEMO_APR, interest + 1) is not None


@pytest.mark.parametrize("balance,payment", [
    (3204.18, 150.0), (1000.0, 100.0), (99.99, 25.0), (500.0, 500.0),
])
def test_zero_apr_is_plain_division(balance, payment):
    result = calculate_payoff(balance, 0.0, payment)
    assert result["months"] == math.ceil(balance / payment)
    assert result["total_interest"] == 0.0


def test_zero_balance_is_zero_months():
    result = calculate_payoff(0.0, DEMO_APR, 150.0)
    assert result == {"months": 0, "total_interest": 0.0, "series": []}


# the minimum payment ---------------------------------------------------------

def test_minimum_payment_is_interest_plus_one_percent():
    interest = DEMO_BALANCE * DEMO_APR / 100 / 12
    assert minimum_payment(DEMO_BALANCE, DEMO_APR) == pytest.approx(
        interest + 0.01 * DEMO_BALANCE, abs=0.01)


@pytest.mark.parametrize("apr", [0, 5, 12, 18, 24, 24.99, 29.99, 36, 49.9, 79.9])
def test_the_minimum_always_amortizes_at_any_apr(apr):
    """The property the spec's max(25, 2%) lacks.

    A flat percentage drops below one month's interest once APR exceeds 12x
    that percentage -- 2% breaks above 24% APR, which is why the 24.99% demo
    card returned None. Interest + 1% of principal is above interest by
    construction, so there is no APR at which this fails.
    """
    payment = minimum_payment(DEMO_BALANCE, apr)
    assert payment > DEMO_BALANCE * apr / 100 / 12
    assert calculate_payoff(DEMO_BALANCE, apr, payment) is not None


def test_the_spec_formula_would_have_failed_here():
    """Pins the reason for the deviation so it cannot be quietly reverted."""
    spec_payment = max(25, 0.02 * DEMO_BALANCE)
    assert spec_payment == pytest.approx(64.08, abs=0.01)
    assert calculate_payoff(DEMO_BALANCE, DEMO_APR, spec_payment) is None


def test_small_balances_hit_the_twenty_five_dollar_floor():
    assert minimum_payment(50.0, 24.99) == 25.0


# payment_for_months ----------------------------------------------------------

@pytest.mark.parametrize("months", [6, 12, 18, 24, 36, 48, 60])
def test_payment_for_months_hits_its_target_exactly(months):
    payment = payment_for_months(DEMO_BALANCE, DEMO_APR, months)
    assert calculate_payoff(DEMO_BALANCE, DEMO_APR, payment)["months"] == months


def test_payment_for_months_rounds_up_not_to_nearest():
    """Rounding to nearest can land a fraction of a cent short, and the
    amortization then bills an extra month: 304.5232 -> 304.52 pays off in 13
    months, not 12."""
    exact = DEMO_BALANCE * (DEMO_APR / 100 / 12) / (
        1 - (1 + DEMO_APR / 100 / 12) ** -12)
    payment = payment_for_months(DEMO_BALANCE, DEMO_APR, 12)

    assert payment >= exact
    assert payment == 304.53
    assert round(exact, 2) == 304.52, "the naive rounding this guards against"
    assert calculate_payoff(DEMO_BALANCE, DEMO_APR, 304.52)["months"] == 13


def test_payment_for_months_at_zero_apr():
    assert payment_for_months(1200.0, 0.0, 12) == 100.0


# the scenario set ------------------------------------------------------------

def test_four_scenarios_on_the_demo_card():
    scenarios = payoff_scenarios(DEMO_BALANCE, DEMO_APR)

    assert [s["label"] for s in scenarios] == [
        "Minimum only", "Pay off in 3 years", "Minimum + $100", "Pay off in 1 year"]
    assert [s["months"] for s in scenarios] == [55, 36, 20, 12]
    assert scenarios[0]["monthly_payment"] == 98.77


def test_scenarios_are_ordered_by_payment_and_save_more_as_they_rise():
    scenarios = payoff_scenarios(DEMO_BALANCE, DEMO_APR)
    payments = [s["monthly_payment"] for s in scenarios]
    interest = [s["total_interest"] for s in scenarios]

    assert payments == sorted(payments)
    assert interest == sorted(interest, reverse=True), "paying more must cost less"


def test_every_series_ends_at_zero_and_matches_its_month_count():
    for s in payoff_scenarios(DEMO_BALANCE, DEMO_APR):
        assert len(s["series"]) == s["months"]
        assert s["series"][-1]["balance"] == 0
        balances = [p["balance"] for p in s["series"]]
        assert balances == sorted(balances, reverse=True), "balance must only fall"


def test_near_identical_payments_do_not_produce_duplicate_lines():
    """Two rules can land within a dollar of each other; one line, not two."""
    for balance, apr in [(3204.18, 24.99), (800.0, 19.99), (12000.0, 14.5)]:
        payments = [s["monthly_payment"] for s in payoff_scenarios(balance, apr)]
        for a, b in zip(payments, payments[1:]):
            assert b - a >= 1.0, f"{a} and {b} are the same line"


def test_scenarios_validate_against_the_frozen_model():
    from models import PayoffScenario

    for s in payoff_scenarios(DEMO_BALANCE, DEMO_APR):
        model = PayoffScenario(**s)
        assert model.label
        assert model.series[-1].balance == 0


def test_payoff_scenario_still_validates_without_a_label():
    """The freeze permits adding a field, not breaking one."""
    from models import PayoffScenario

    assert PayoffScenario(monthly_payment=150.0, months=1, total_interest=0.0,
                          series=[{"month": 1, "balance": 0.0}]).label == ""


# robustness -------------------------------------------------------------------

@pytest.mark.parametrize("balance,apr", [
    (0, 24.99), (-100, 24.99), (None, 24.99), (3204.18, None), ("junk", 24.99),
])
def test_scenarios_degrade_to_empty_rather_than_raising(balance, apr):
    assert payoff_scenarios(balance, apr) == []


@pytest.mark.parametrize("args", [
    (None, 24.99, 150.0), (3204.18, None, 150.0), (3204.18, 24.99, None),
    ("x", "y", "z"), (3204.18, 24.99, 0), (3204.18, 24.99, -50), (3204.18, -5, 150),
])
def test_calculate_payoff_returns_none_on_junk_rather_than_raising(args):
    assert calculate_payoff(*args) is None


def test_a_balance_that_never_clears_within_the_cap_returns_none():
    """Fifty years of payments and still owing is 'never pays off'."""
    interest = 500_000 * 0.2999 / 100 / 12
    assert calculate_payoff(500_000, 29.99, interest + 0.02) is None


def test_results_are_deterministic():
    first = payoff_scenarios(DEMO_BALANCE, DEMO_APR)
    for _ in range(5):
        assert payoff_scenarios(DEMO_BALANCE, DEMO_APR) == first
