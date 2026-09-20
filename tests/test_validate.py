"""Stage 2 reconciliation tests. See CLAUDE.md section 5, Stage 2."""

import copy
import json
from pathlib import Path

import pytest

from adapter import TOTALS_DERIVED, TOTALS_STATEMENT, adapt
from validate import balance_sign, reconcile

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def load(name: str) -> tuple[dict, list[dict]]:
    return adapt(json.loads((FIXTURES / f"{name}.json").read_text()))


@pytest.fixture
def checking():
    return load("checking")


@pytest.fixture
def credit():
    return load("credit")


# --- the happy path --------------------------------------------------------

def test_clean_checking_fixture_reconciles(checking):
    acct, txns = checking
    result = reconcile(acct, txns)

    assert result["reconciled"] is True
    assert result["delta"] == 0.0
    assert result["rows_needing_review"] == []
    assert result["checks"] == {"sum_vs_balance": True,
                                "totals_vs_balance": True,
                                "running_balance": True}
    assert result["checks_run"] == 3


def test_clean_credit_fixture_reconciles(credit):
    """The sign fix. A credit balance is a liability and moves against the
    amount, so without the sign factor all three checks fail on real data."""
    acct, txns = credit
    result = reconcile(acct, txns)

    assert acct["account_type"] == "credit"
    assert result["reconciled"] is True
    assert result["delta"] == 0.0
    assert result["rows_needing_review"] == []
    assert result["checks_run"] == 3


def test_credit_would_fail_if_treated_as_an_asset(credit):
    """Proves the sign factor is load-bearing, not decorative."""
    acct, txns = credit
    mislabelled = {**acct, "account_type": "checking"}
    result = reconcile(mislabelled, txns)

    assert result["reconciled"] is False
    assert result["checks"]["sum_vs_balance"] is False
    # Off by exactly twice the true delta, the signature of a sign inversion.
    assert result["delta"] == pytest.approx(2 * abs(sum(t["amount"] for t in txns)), abs=0.01)


def test_balance_sign_mapping():
    assert balance_sign("credit") == -1
    for asset in ("checking", "savings", "unknown", None):
        assert balance_sign(asset) == 1


# --- CLAUDE.md's required cases -------------------------------------------

def test_altered_amount_is_caught_and_the_row_is_flagged(checking):
    acct, txns = checking
    txns = copy.deepcopy(txns)
    txns[7]["amount"] = round(txns[7]["amount"] - 40.00, 2)

    result = reconcile(acct, txns)

    assert result["reconciled"] is False
    assert result["checks"]["running_balance"] is False
    assert 7 in result["rows_needing_review"]
    # Row 8's balance is still consistent with row 7's, so only the tampered
    # row is blamed -- the check localizes the fault rather than smearing it.
    assert result["rows_needing_review"] == [7]


def test_deleted_row_fails_check_a_with_delta_equal_to_that_row(checking):
    acct, txns = checking
    txns = copy.deepcopy(txns)
    removed = txns.pop(12)

    result = reconcile(acct, txns)

    assert result["reconciled"] is False
    assert result["checks"]["sum_vs_balance"] is False
    assert result["delta"] == pytest.approx(abs(removed["amount"]), abs=0.01)


def test_null_opening_balance_skips_a_and_b_but_c_still_runs(checking):
    acct, txns = checking
    acct = {**acct, "opening_balance": None}

    result = reconcile(acct, txns)

    assert result["checks"]["sum_vs_balance"] is None
    assert result["checks"]["totals_vs_balance"] is None
    assert result["checks"]["running_balance"] is True
    assert result["delta"] == 0.0
    assert result["checks_run"] == 1
    assert result["reconciled"] is True


def test_null_balances_skip_c_but_a_still_runs(checking):
    acct, txns = checking
    txns = [{**t, "balance": None} for t in txns]

    result = reconcile(acct, txns)

    assert result["checks"]["running_balance"] is None
    assert result["checks"]["sum_vs_balance"] is True
    assert result["checks"]["totals_vs_balance"] is True
    assert result["reconciled"] is True


# --- the opening-balance anchor -------------------------------------------

def test_first_row_is_validated_against_opening_balance(checking):
    """Without the anchor the first row is checked by nothing but A."""
    acct, txns = checking
    txns = copy.deepcopy(txns)
    txns[0]["amount"] = round(txns[0]["amount"] + 15.00, 2)

    result = reconcile(acct, txns)

    assert 0 in result["rows_needing_review"]
    assert result["checks"]["running_balance"] is False


def test_the_anchor_localizes_what_check_a_can_only_detect(checking):
    """What the anchor is actually for.

    A row-0 error is always *detected*, by check A. But A is a whole-statement
    sum: it reports "you are off by 1842.55 somewhere" and cannot say where.
    Only check C names a row, and without the opening anchor C never looks at
    row 0 -- so it reports all-clear and the user is told something is wrong
    with no indication of what. Same tampering, both ways round:
    """
    acct, txns = checking
    txns = copy.deepcopy(txns)
    txns[0]["amount"] = -txns[0]["amount"]

    anchored = reconcile(acct, txns)
    unanchored = reconcile({**acct, "opening_balance": None}, txns)

    # Both know something is wrong -- but only one of them knows what.
    assert anchored["reconciled"] is False
    assert anchored["checks"]["sum_vs_balance"] is False
    assert anchored["rows_needing_review"] == [0]

    assert unanchored["checks"]["running_balance"] is True, "C sees nothing"
    assert unanchored["rows_needing_review"] == [], "and can blame nobody"


def test_a_uniformly_shifted_statement_is_not_an_error(checking):
    """The anchor adds localization, not omniscience -- worth pinning down so
    nobody later mistakes this for a gap.

    Flip row 0 and shift every printed balance to match, and the result is a
    perfectly self-consistent statement describing a different history. No
    arithmetic check can reject it, because there is nothing arithmetically
    wrong with it. Catching this needs evidence from outside the statement.
    """
    acct, txns = checking
    txns = copy.deepcopy(txns)
    shift = 2 * txns[0]["amount"]
    txns[0]["amount"] = -txns[0]["amount"]
    for txn in txns:
        txn["balance"] = round(txn["balance"] - shift, 2)
    acct = {**acct, "closing_balance": round(acct["closing_balance"] - shift, 2),
            "total_deposits": None, "total_withdrawals": None}

    result = reconcile(acct, txns)

    assert result["reconciled"] is True
    assert result["rows_needing_review"] == []


def test_anchor_absent_when_opening_balance_is_null_but_c_still_works(checking):
    """Degrades to plain consecutive-pair checking. Must not crash."""
    acct, txns = checking
    acct = {**acct, "opening_balance": None}
    txns = copy.deepcopy(txns)
    txns[0]["amount"] = round(txns[0]["amount"] + 15.00, 2)

    result = reconcile(acct, txns)

    assert 0 not in result["rows_needing_review"], "row 0 is unanchored, so unchecked"
    assert result["checks"]["running_balance"] is True


def test_anchor_works_on_credit_too(credit):
    acct, txns = credit
    txns = copy.deepcopy(txns)
    txns[0]["amount"] = round(txns[0]["amount"] - 9.99, 2)

    assert 0 in reconcile(acct, txns)["rows_needing_review"]


# --- check C chain behaviour ----------------------------------------------

def test_missing_balance_breaks_the_chain_on_both_sides(checking):
    acct, txns = checking
    txns = copy.deepcopy(txns)
    txns[5]["balance"] = None

    result = reconcile(acct, txns)

    # Pairs (4,5) and (5,6) are both unevaluable; nothing is falsely blamed.
    assert result["rows_needing_review"] == []
    assert result["checks"]["running_balance"] is True


def test_gap_is_not_bridged_by_carrying_the_last_balance_forward(checking):
    """If row 5 has no balance, row 6 must not be compared against row 4."""
    acct, txns = checking
    txns = copy.deepcopy(txns)
    txns[5]["balance"] = None
    txns[6]["amount"] = round(txns[6]["amount"] + 50.00, 2)

    result = reconcile(acct, txns)

    assert 6 not in result["rows_needing_review"]


def test_tolerance_is_one_cent(checking):
    acct, txns = checking
    txns = copy.deepcopy(txns)

    txns[3]["amount"] = round(txns[3]["amount"] + 0.01, 2)
    assert reconcile(acct, txns)["rows_needing_review"] == [], "exactly 1c is within tolerance"

    txns[3]["amount"] = round(txns[3]["amount"] + 0.01, 2)
    assert 3 in reconcile(acct, txns)["rows_needing_review"], "2c is not"


# --- check B independence --------------------------------------------------

def test_header_totals_can_fail_while_rows_are_fine(checking):
    acct, txns = checking
    acct = {**acct, "total_deposits": acct["total_deposits"] + 100.00}

    result = reconcile(acct, txns)

    assert result["checks"]["totals_vs_balance"] is False
    assert result["checks"]["sum_vs_balance"] is True
    assert result["checks"]["running_balance"] is True
    assert result["reconciled"] is False


def test_missing_header_totals_skip_b_only(checking):
    acct, txns = checking
    acct = {**acct, "total_deposits": None, "total_withdrawals": None}

    result = reconcile(acct, txns)

    assert result["checks"]["totals_vs_balance"] is None
    assert result["reconciled"] is True
    assert result["checks_run"] == 2


# --- derived totals must not be allowed to audit themselves ---------------

def test_derived_totals_do_not_run_check_b():
    """The whole reason totals_source exists.

    The adapter fills unprinted totals from our own rows, so
    deposits - withdrawals is identically sum(amounts) -- which is what check
    A already compares against closing - opening. Letting B run on those would
    restate A, agree with it unconditionally, and advertise three passing
    checks on one piece of evidence.
    """
    raw = json.loads((FIXTURES / "checking.json").read_text())
    acct, txns = adapt({**raw, "total_deposits": None, "total_withdrawals": None})

    assert acct["totals_source"] == TOTALS_DERIVED
    assert acct["total_deposits"] is not None, "the numbers are still filled in"

    result = reconcile(acct, txns)

    assert result["checks"]["totals_vs_balance"] is None
    assert result["checks_run"] == 2, "derived totals add no evidence"
    assert result["reconciled"] is True


def test_a_derived_check_b_is_just_check_a_wearing_a_hat():
    """Proves the circularity rather than asserting it.

    Corrupt one row's withdrawal. Then compare what check B reports in each
    provenance, against what check A reports on the same data:

      printed  A fails, B PASSES. B compares two header figures and never
               looked at our rows, so it is untouched -- and that gap between
               A and B is information: the header is self-consistent, so the
               fault is in our extraction.

      derived  A fails, B fails identically, because B was computed from the
               very rows A is rejecting. It has no opinion of its own.

    Two checks that cannot disagree are one check.
    """
    raw = json.loads((FIXTURES / "checking.json").read_text())
    tampered = copy.deepcopy(raw)
    tampered["transactions"][4]["withdrawal"] = 9999.99

    printed = reconcile(*adapt(tampered))
    assert printed["checks"]["sum_vs_balance"] is False
    assert printed["checks"]["totals_vs_balance"] is True, "header still agrees with itself"

    # Smuggle derived totals past the provenance gate -- exactly what
    # validate.py refuses to do -- and watch B collapse onto A.
    derived_acct, derived_txns = adapt(
        {**tampered, "total_deposits": None, "total_withdrawals": None})
    smuggled = reconcile({**derived_acct, "totals_source": TOTALS_STATEMENT}, derived_txns)

    assert smuggled["checks"]["totals_vs_balance"] == smuggled["checks"]["sum_vs_balance"]
    assert smuggled["checks"]["totals_vs_balance"] is False


@pytest.mark.parametrize("tamper", [
    {"transactions": 4, "withdrawal": 9999.99},
    {"transactions": 0, "withdrawal": 0.01},
    {"transactions": 11, "deposit": 500.00},
])
def test_derived_b_tracks_a_exactly_whatever_we_break(tamper):
    """The identity holds for any corruption, which is what makes it useless."""
    raw = copy.deepcopy(json.loads((FIXTURES / "checking.json").read_text()))
    index = tamper.pop("transactions")
    raw["transactions"][index].update(tamper)
    raw["total_deposits"] = raw["total_withdrawals"] = None

    acct, txns = adapt(raw)
    result = reconcile({**acct, "totals_source": TOTALS_STATEMENT}, txns)

    assert result["checks"]["totals_vs_balance"] == result["checks"]["sum_vs_balance"]


def test_absent_provenance_is_treated_as_printed(checking):
    """Back-compat: a hand-built account dict with no totals_source key, as in
    the tests above and anything predating this field, still runs check B."""
    acct, txns = checking
    acct = {k: v for k, v in acct.items() if k != "totals_source"}

    assert reconcile(acct, txns)["checks"]["totals_vs_balance"] is True


# --- no evidence is not the same as verified ------------------------------

def test_a_statement_with_nothing_checkable_is_not_reconciled():
    acct = {"id": "x", "account_type": "checking", "opening_balance": None,
            "closing_balance": None, "total_deposits": None, "total_withdrawals": None}
    txns = [{"amount": -5.0, "balance": None}, {"amount": -6.0, "balance": None}]

    result = reconcile(acct, txns)

    assert result["checks_run"] == 0
    assert all(v is None for v in result["checks"].values())
    assert result["reconciled"] is False, "no evidence must not read as verified"


# --- crash safety ----------------------------------------------------------

def test_empty_transaction_list_does_not_crash(checking):
    acct, _ = checking
    result = reconcile(acct, [])

    assert result["checks"]["sum_vs_balance"] is None, "no rows, nothing to sum"
    assert result["checks"]["running_balance"] is None
    assert result["rows_needing_review"] == []


@pytest.mark.parametrize("acct,txns", [
    (None, None),
    ({}, []),
    (None, [{"amount": -1.0, "balance": 5.0}]),
    ({"account_type": "credit"}, None),
    ("not a dict", "not a list"),
    ({}, [None, "junk", 42, {"amount": -1.0}]),
])
def test_malformed_input_degrades_instead_of_raising(acct, txns):
    result = reconcile(acct, txns)

    assert result["reconciled"] is False
    assert result["delta"] == 0.0
    assert result["rows_needing_review"] == []
    assert set(result["checks"]) == {"sum_vs_balance", "totals_vs_balance", "running_balance"}


@pytest.mark.parametrize("junk", ["N/A", "", float("nan"), float("inf"), None, {}, [], True])
def test_non_numeric_fields_are_treated_as_missing_not_as_zero(junk, checking):
    """A parser that hallucinated a balance can hallucinate 'N/A' just as
    easily. Coercing junk to 0.0 would invent arithmetic errors."""
    acct, txns = checking
    txns = copy.deepcopy(txns)
    txns[9]["balance"] = junk

    result = reconcile(acct, txns)

    assert result["rows_needing_review"] == []
    assert result["checks"]["sum_vs_balance"] is True


@pytest.mark.parametrize("junk", ["N/A", float("nan"), float("inf"), None])
def test_junk_in_account_headers_skips_checks_rather_than_crashing(junk, checking):
    acct, txns = checking
    result = reconcile({**acct, "opening_balance": junk}, txns)

    assert result["checks"]["sum_vs_balance"] is None
    assert result["checks"]["totals_vs_balance"] is None
    assert result["delta"] == 0.0


def test_string_numbers_are_still_usable(checking):
    """A parser emitting "2450.00" instead of 2450.00 should not lose a check."""
    acct, txns = checking
    acct = {**acct, "opening_balance": str(acct["opening_balance"])}

    assert reconcile(acct, txns)["checks"]["sum_vs_balance"] is True


def test_result_shape_is_stable(checking):
    acct, txns = checking
    result = reconcile(acct, txns)

    assert set(result) == {"reconciled", "delta", "checks",
                           "rows_needing_review", "checks_run"}
    assert isinstance(result["reconciled"], bool)
    assert isinstance(result["delta"], float)
    assert isinstance(result["rows_needing_review"], list)
    assert all(isinstance(i, int) for i in result["rows_needing_review"])
