"""Stage 6 tests. See CLAUDE.md section 5, Stage 6."""

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from adapter import adapt
from transfers import find_transfer_pairs, mark_transfers

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def txn(account, amount, day, description="TRANSACTION", **extra):
    return {"account_id": account, "amount": amount,
            "date": date(2026, 8, day), "description": description,
            "is_transfer": False, **extra}


# --- CLAUDE.md's six required cases ---------------------------------------

def test_equal_and_opposite_three_days_apart_on_two_accounts_pairs():
    txns = [txn("checking", -450.00, 20, "WEB BILL PAYMENT - VISA"),
            txn("credit", 450.00, 23, "AUTOMATIC PAYMENT - THANK YOU")]
    assert find_transfer_pairs(txns) == [(0, 1)]


def test_same_account_does_not_pair():
    txns = [txn("checking", -450.00, 20), txn("checking", 450.00, 23)]
    assert find_transfer_pairs(txns) == []


def test_amounts_two_cents_apart_do_not_pair():
    txns = [txn("checking", -450.00, 20), txn("credit", 450.02, 21)]
    assert find_transfer_pairs(txns) == []


def test_amounts_one_cent_apart_still_pair():
    """The tolerance is a cent, and the bucketing must not quietly lose it."""
    txns = [txn("checking", -450.00, 20), txn("credit", 450.01, 21)]
    assert find_transfer_pairs(txns) == [(0, 1)]


def test_ten_days_apart_does_not_pair():
    txns = [txn("checking", -450.00, 1), txn("credit", 450.00, 11)]
    assert find_transfer_pairs(txns) == []


def test_keyword_breaks_a_tie_between_two_candidates():
    txns = [txn("checking", -450.00, 20, "SOMETHING ELSE"),
            txn("credit", 450.00, 20, "REF 88213"),
            txn("credit", 450.00, 20, "AUTOMATIC PAYMENT - THANK YOU")]
    assert find_transfer_pairs(txns) == [(0, 2)]


def test_each_transaction_is_used_at_most_once():
    txns = [txn("checking", -450.00, 20), txn("credit", 450.00, 20),
            txn("savings", 450.00, 20), txn("credit", 450.00, 21)]
    pairs = find_transfer_pairs(txns)
    flat = [index for pair in pairs for index in pair]
    assert len(flat) == len(set(flat))


# --- the matching rules in isolation --------------------------------------

def test_same_sign_never_pairs():
    """Two withdrawals of the same size are not a transfer."""
    assert find_transfer_pairs([txn("checking", -450.00, 20),
                                txn("credit", -450.00, 20)]) == []
    assert find_transfer_pairs([txn("checking", 450.00, 20),
                                txn("credit", 450.00, 20)]) == []


def test_window_boundary_is_inclusive():
    assert find_transfer_pairs([txn("a", -100.0, 1), txn("b", 100.0, 4)]) == [(0, 1)]
    assert find_transfer_pairs([txn("a", -100.0, 1), txn("b", 100.0, 5)]) == []


def test_window_is_configurable():
    txns = [txn("a", -100.0, 1), txn("b", 100.0, 11)]
    assert find_transfer_pairs(txns) == []
    assert find_transfer_pairs(txns, window_days=10) == [(0, 1)]


def test_closest_date_wins_when_no_keyword_distinguishes():
    txns = [txn("checking", -300.00, 15, "REF 1"),
            txn("credit", 300.00, 18, "REF 2"),
            txn("credit", 300.00, 16, "REF 3")]
    assert find_transfer_pairs(txns) == [(0, 2)]


def test_keyword_beats_a_closer_date():
    """Keywords rank above date distance, per CLAUDE.md's ordering."""
    txns = [txn("checking", -300.00, 15, "REF 1"),
            txn("credit", 300.00, 16, "REF 2"),
            txn("credit", 300.00, 17, "ONLINE PMT")]
    assert find_transfer_pairs(txns) == [(0, 2)]


@pytest.mark.parametrize("keyword", [
    "PAYMENT", "TRANSFER", "THANK YOU", "XFER", "ZELLE", "VENMO", "ONLINE PMT", "AUTOPAY",
])
def test_every_documented_keyword_acts_as_a_tiebreaker(keyword):
    txns = [txn("checking", -75.00, 10, "REF A"),
            txn("credit", 75.00, 10, "REF B"),
            txn("credit", 75.00, 10, f"SOME {keyword} HERE")]
    assert find_transfer_pairs(txns) == [(0, 2)]


def test_keywords_are_never_required():
    """A transfer described as nothing but a reference number still pairs."""
    txns = [txn("checking", -820.55, 3, "REF 99812"),
            txn("savings", 820.55, 4, "DEP 41823")]
    assert find_transfer_pairs(txns) == [(0, 1)]


def test_keyword_matching_is_case_insensitive():
    txns = [txn("checking", -50.0, 5, "ref"),
            txn("credit", 50.0, 5, "ref"),
            txn("credit", 50.0, 5, "online pmt received")]
    assert find_transfer_pairs(txns) == [(0, 2)]


# --- determinism -----------------------------------------------------------

def test_result_does_not_depend_on_input_order():
    """Pairs are ranked globally, not matched greedily in list order, so the
    order two statements happened to be concatenated in cannot change it."""
    a = txn("checking", -450.00, 20, "PAYMENT")
    b = txn("credit", 450.00, 21, "THANK YOU")
    c = txn("checking", -60.00, 5, "GROCERIES")

    forward = find_transfer_pairs([a, b, c])
    backward = find_transfer_pairs([c, b, a])

    assert len(forward) == len(backward) == 1
    paired_forward = {[a, b, c][i]["description"] for i in forward[0]}
    paired_backward = {[c, b, a][i]["description"] for i in backward[0]}
    assert paired_forward == paired_backward == {"PAYMENT", "THANK YOU"}


def test_repeated_calls_agree():
    txns = [txn("checking", -450.00, 20), txn("credit", 450.00, 21),
            txn("savings", 450.00, 22), txn("credit", -450.00, 20)]
    results = {tuple(find_transfer_pairs(txns)) for _ in range(20)}
    assert len(results) == 1


# --- mark_transfers --------------------------------------------------------

def test_mark_transfers_flags_both_sides():
    txns = [txn("checking", -450.00, 20), txn("credit", 450.00, 21),
            txn("credit", -12.50, 21, "COFFEE")]
    mark_transfers(txns)
    assert txns[0]["is_transfer"] is True
    assert txns[1]["is_transfer"] is True
    assert txns[2]["is_transfer"] is False


def test_mark_transfers_leaves_untouched_rows_false():
    txns = [txn("checking", -10.0, 1, "COFFEE"), txn("credit", -20.0, 2, "LUNCH")]
    mark_transfers(txns)
    assert not any(t["is_transfer"] for t in txns)


def test_mark_transfers_adds_the_flag_when_absent():
    txns = [{"account_id": "a", "amount": -5.0, "date": date(2026, 8, 1), "description": "X"}]
    mark_transfers(txns)
    assert txns[0]["is_transfer"] is False


# --- robustness ------------------------------------------------------------

@pytest.mark.parametrize("txns", [
    [], [txn("a", -1.0, 1)], None, "not a list",
    [None, "junk", 42],
    [{"account_id": "a"}, {"account_id": "b"}],
    [txn("a", None, 1), txn("b", 100.0, 1)],
])
def test_malformed_input_does_not_raise(txns):
    assert find_transfer_pairs(txns) == []


def test_unparseable_dates_are_skipped_not_crashed():
    txns = [{"account_id": "a", "amount": -100.0, "date": "not a date", "description": "X"},
            {"account_id": "b", "amount": 100.0, "date": date(2026, 8, 1), "description": "Y"}]
    assert find_transfer_pairs(txns) == []


def test_string_dates_are_accepted():
    txns = [{"account_id": "a", "amount": -100.0, "date": "2026-08-01", "description": "X",
             "is_transfer": False},
            {"account_id": "b", "amount": 100.0, "date": "2026-08-02", "description": "Y",
             "is_transfer": False}]
    assert find_transfer_pairs(txns) == [(0, 1)]


def test_zero_amounts_never_pair():
    assert find_transfer_pairs([txn("a", 0.0, 1), txn("b", 0.0, 1)]) == []


def test_many_identical_amounts_stay_within_budget():
    """Bucketing keeps this from exploding; also proves one-use-only at scale."""
    txns = ([txn(f"out{i}", -25.00, 1 + i % 3) for i in range(60)] +
            [txn(f"in{i}", 25.00, 1 + i % 3) for i in range(60)])
    pairs = find_transfer_pairs(txns)
    flat = [index for pair in pairs for index in pair]
    assert len(flat) == len(set(flat))
    assert len(pairs) <= 60


# --- the real fixtures -----------------------------------------------------

def test_the_planted_pair_is_found_across_both_statements():
    txns = []
    for name in ("checking", "credit"):
        _, rows = adapt(json.loads((FIXTURES / f"{name}.json").read_text()))
        txns += rows

    pairs = find_transfer_pairs(txns)
    assert len(pairs) == 1

    left, right = pairs[0]
    assert txns[left]["account_id"] != txns[right]["account_id"]
    assert abs(txns[left]["amount"] + txns[right]["amount"]) <= 0.01
    assert abs(txns[left]["amount"]) == 450.00


def test_spending_drops_by_exactly_the_transfer_total():
    """CLAUDE.md's manual check, as a test."""
    txns = []
    for name in ("checking", "credit"):
        _, rows = adapt(json.loads((FIXTURES / f"{name}.json").read_text()))
        txns += rows

    before = abs(sum(t["amount"] for t in txns if t["amount"] < 0))
    mark_transfers(txns)
    after = abs(sum(t["amount"] for t in txns if t["amount"] < 0 and not t["is_transfer"]))
    excluded = sum(abs(t["amount"]) for t in txns if t["is_transfer"] and t["amount"] < 0)

    assert after == pytest.approx(before - excluded, abs=0.01)
    assert excluded == pytest.approx(450.00, abs=0.01)


def test_excluding_transfers_leaves_net_unchanged():
    """The invariant that proves this removes double-counting rather than money.

    A transfer contributes -450 to one account and +450 to another, so it
    nets to zero. Dropping both sides must therefore move spending and income
    by the same amount and leave net exactly where it was. If net shifts, we
    have deleted a real transaction rather than a duplicate.
    """
    txns = []
    for name in ("checking", "credit"):
        _, rows = adapt(json.loads((FIXTURES / f"{name}.json").read_text()))
        txns += rows

    net_before = sum(t["amount"] for t in txns)
    mark_transfers(txns)
    net_after = sum(t["amount"] for t in txns if not t["is_transfer"])

    assert net_after == pytest.approx(net_before, abs=0.01)


def test_a_one_sided_match_is_never_paired():
    """Only both halves together net to zero, so a lone leg must not be
    flagged -- doing so would move net and understate real spending."""
    txns = [txn("checking", -450.00, 20, "WEB BILL PAYMENT - VISA")]
    mark_transfers(txns)
    assert txns[0]["is_transfer"] is False
