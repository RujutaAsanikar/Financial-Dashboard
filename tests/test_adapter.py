"""Stage 1 adapter tests. See CLAUDE.md sections 2 and 3."""

import json
from datetime import date
from pathlib import Path

import pytest

from adapter import (
    PERSISTED_ACCOUNT_FIELDS,
    TOTALS_DERIVED,
    TOTALS_STATEMENT,
    VALIDATION_ONLY_ACCOUNT_FIELDS,
    adapt,
    build_account_id,
    last4,
    normalize_account_type,
)

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def one(**row) -> dict:
    """A minimal statement wrapping a single transaction row."""
    base = {"date": "2026-08-01", "description": "TEST ROW",
            "reference": None, "withdrawal": None, "deposit": None, "balance": None}
    return {"bank_name": "Test Bank", "account_number": "****-1234",
            "account_type": "Checking", "statement_period_start": "2026-08-01",
            "transactions": [{**base, **row}]}


# --- amount sign convention ------------------------------------------------

def test_withdrawal_only_is_negative():
    _, txns = adapt(one(withdrawal=25.50))
    assert txns[0]["amount"] == -25.50


def test_deposit_only_is_positive():
    _, txns = adapt(one(deposit=1842.55))
    assert txns[0]["amount"] == 1842.55


def test_both_columns_filled_returns_net():
    _, txns = adapt(one(withdrawal=100.00, deposit=30.00))
    assert txns[0]["amount"] == -70.00

    _, txns = adapt(one(withdrawal=30.00, deposit=100.00))
    assert txns[0]["amount"] == 70.00


def test_amount_rounded_to_two_places():
    _, txns = adapt(one(withdrawal=10.005, deposit=0.001))
    assert txns[0]["amount"] == round(0.001 - 10.005, 2)


# --- account number redaction ---------------------------------------------

def test_full_account_number_reduced_to_last4():
    acct, _ = adapt({"account_number": "00012-345-678-9", "bank_name": "Keystone",
                     "transactions": []})
    assert acct["account_last4"] == "6789"


def test_full_account_number_absent_from_entire_output():
    """The single most important assertion in this stage."""
    raw = load("checking")
    full = raw["account_number"]
    acct, txns = adapt(raw)

    serialized = json.dumps({"account": acct, "txns": txns}, default=str)
    assert full not in serialized
    assert "00012-345-678-9" not in serialized
    assert "000123456789" not in serialized
    assert acct["account_last4"] == "6789"


def test_last4_helper_edge_cases():
    assert last4(None) is None
    assert last4("") is None
    assert last4("****-****-****-4821") == "4821"
    assert last4("no digits here") is None
    assert last4("7") == "7"


# --- account_type normalization -------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Chequing", "checking"),
    ("Chequing Account", "checking"),
    ("Checking", "checking"),
    ("CHECKING", "checking"),
    ("Savings", "savings"),
    ("Saving", "savings"),
    ("Credit Card", "credit"),
    ("Visa", "credit"),
    ("Mastercard", "credit"),
    ("Amex", "credit"),
    ("Gibberish", "unknown"),
    ("", "unknown"),
    (None, "unknown"),
])
def test_account_type_normalization(raw, expected):
    assert normalize_account_type(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    # Card brands as actually printed, i.e. never on their own
    ("Visa Signature", "credit"),
    ("Visa Infinite Privilege", "credit"),
    ("World Mastercard", "credit"),
    ("American Express Platinum", "credit"),
    ("Discover it Card", "credit"),
    ("Credit Card Account", "credit"),
    ("Line of Credit", "credit"),
    ("Personal Line Of Credit", "credit"),
    ("HELOC", "credit"),
    ("Charge Card", "credit"),
    # Deposit accounts as actually printed
    ("Chequing Account - CAD", "checking"),
    ("Everyday Chequing", "checking"),
    ("Student Chequing Account", "checking"),
    ("Free Interest Checking", "checking"),
    ("Current Account", "checking"),
    ("Demand Deposit Account", "checking"),
    ("High Yield Savings", "savings"),
    ("Money Market Account", "savings"),
    ("TFSA", "savings"),
    ("Certificate of Deposit", "savings"),
    # Whitespace and case are already handled, but prove it on real strings
    ("  visa   SIGNATURE  ", "credit"),
])
def test_account_type_keyword_matching(raw, expected):
    """Statements print qualified names, so exact matching is not enough."""
    assert normalize_account_type(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    # TRAP 1: the card network does not make it a credit account.
    ("Visa Debit", "checking"),
    ("Visa Debit Card", "checking"),
    ("Debit Mastercard", "checking"),
    # TRAP 2: "credit" here names the institution, not the product.
    ("Credit Union Checking", "checking"),
    ("Acme Credit Union Chequing Account", "checking"),
    ("Credit Union Savings", "savings"),
    # ...but a credit union's actual card is still a card.
    ("Acme Credit Union Visa", "credit"),
    ("Credit Union Credit Card", "credit"),
])
def test_account_type_traps(raw, expected):
    """The two misreadings that would silently invert reconciliation."""
    assert normalize_account_type(raw) == expected


@pytest.mark.parametrize("raw", [
    "Statement of Account",
    "Prepaid Card",
    "Konto",
    "Account Summary",
    "12345",
])
def test_unrecognised_types_fall_through_to_unknown(raw):
    """Biased against claiming credit: an unknown defaults to asset semantics,
    which degrades visibly rather than silently inverting reconciliation."""
    assert normalize_account_type(raw) == "unknown"


def test_account_type_is_always_one_of_four():
    for raw in ("Visa", "Chequing", "Savings", "Gibberish", None, "", "  "):
        assert normalize_account_type(raw) in {"checking", "savings", "credit", "unknown"}


# --- account_id ------------------------------------------------------------

def test_account_id_is_a_slug():
    acct, _ = adapt({"bank_name": "Keystone Savings Bank",
                     "account_number": "****-6789", "transactions": []})
    assert acct["id"] == "keystone-savings-bank-6789"


def test_null_account_number_still_generates_an_id():
    acct, txns = adapt({"bank_name": "Chase", "account_number": None,
                        "statement_period_start": "2026-08-01",
                        "transactions": [{"date": "2026-08-02", "description": "X",
                                          "withdrawal": 5.0, "deposit": None,
                                          "balance": None, "reference": None}]})
    assert acct["account_last4"] is None
    assert acct["id"]
    assert txns[0]["account_id"] == acct["id"]


def test_both_null_falls_back_to_period_hash():
    acct, _ = adapt({"bank_name": None, "account_number": None,
                     "statement_period_start": "2026-08-01", "transactions": []})
    assert acct["id"].startswith("unknown-")


def test_account_id_is_deterministic():
    payload = {"bank_name": None, "account_number": None,
               "statement_period_start": "2026-08-01", "transactions": []}
    assert adapt(payload)[0]["id"] == adapt(payload)[0]["id"]
    assert build_account_id(None, None, "2026-08-01") != build_account_id(None, None, "2026-09-01")


# --- dates -----------------------------------------------------------------

def test_unparseable_date_skips_only_that_row():
    payload = {"bank_name": "B", "account_number": "1234", "transactions": [
        {"date": "2026-08-01", "description": "good one", "withdrawal": 1.0,
         "deposit": None, "balance": None, "reference": None},
        {"date": "not a date", "description": "bad one", "withdrawal": 2.0,
         "deposit": None, "balance": None, "reference": None},
        {"date": "2026-08-03", "description": "good two", "withdrawal": 3.0,
         "deposit": None, "balance": None, "reference": None},
    ]}
    _, txns = adapt(payload)
    assert [t["description"] for t in txns] == ["good one", "good two"]
    assert txns[0]["date"] == date(2026, 8, 1)


def test_missing_and_null_dates_are_skipped():
    payload = {"bank_name": "B", "account_number": "1234", "transactions": [
        {"description": "no date key", "withdrawal": 1.0},
        {"date": None, "description": "null date", "withdrawal": 1.0},
    ]}
    _, txns = adapt(payload)
    assert txns == []


# --- description handling --------------------------------------------------

def test_description_preserved_verbatim():
    ugly = "  SQ *COFFEE TREE ROASTER 04213   PITTSBURGH PA  "
    _, txns = adapt(one(description=ugly, withdrawal=6.25))
    assert txns[0]["description"] == ugly


def test_merchant_is_not_populated_in_stage_1():
    _, txns = adapt(one(withdrawal=1.0))
    assert txns[0]["merchant"] is None
    assert txns[0]["category"] is None
    assert txns[0]["is_transfer"] is False
    assert txns[0]["is_recurring"] is False


def test_every_zero_amount_row_is_skipped():
    """Broader than CLAUDE.md, which skips a zero-amount row only when the
    description is ALSO empty.

    That is too narrow for real extractor output: OCR picks up section
    headings like "Banking/Debit Card Withdrawals and Purchases" as rows with
    plenty of text and no amount. A row that moves no money contributes zero
    to every sum, category and average by definition, so dropping it loses
    nothing and keeps the transaction list honest.
    """
    payload = {"bank_name": "B", "account_number": "1234", "transactions": [
        {"date": "2026-08-01", "description": "   ", "withdrawal": None, "deposit": None},
        {"date": "2026-08-02", "description": "", "withdrawal": 0, "deposit": 0},
        {"date": "2026-08-03", "description": "has text but no amount",
         "withdrawal": None, "deposit": None},
        {"date": "2026-08-04", "description": "real spend", "withdrawal": 12.50,
         "deposit": None},
    ]}
    _, txns = adapt(payload)
    assert [t["description"] for t in txns] == ["real spend"]


def test_a_row_that_nets_to_zero_is_also_skipped():
    """Both columns filled with the same figure moves no money either."""
    _, txns = adapt(one(withdrawal=50.0, deposit=50.0))
    assert txns == []


def test_dropping_zero_rows_does_not_change_any_total():
    """The justification, asserted: a zero row is invisible to every figure,
    so removing it cannot move one."""
    rows = [
        {"date": "2026-08-01", "description": "spend", "withdrawal": 30.0,
         "deposit": None, "balance": None, "reference": None},
        {"date": "2026-08-02", "description": "section heading", "withdrawal": None,
         "deposit": None, "balance": None, "reference": None},
        {"date": "2026-08-03", "description": "income", "withdrawal": None,
         "deposit": 100.0, "balance": None, "reference": None},
    ]
    _, txns = adapt({"bank_name": "B", "account_number": "1234",
                     "statement_period_start": "2026-08-01", "transactions": rows})

    assert len(txns) == 2
    assert sum(t["amount"] for t in txns) == 70.0


# --- the account dict's shape ----------------------------------------------

def test_account_keys_are_exactly_the_declared_two_groups():
    """Pins the shape so it cannot drift away from the docs again.

    total_deposits/total_withdrawals were added for Stage 2 check B and went
    undocumented in both CLAUDE.md section 3 and this repo. If you are here
    because this test failed, you added a key: decide which group it belongs
    to, then update CLAUDE.md section 3 and the adapt() docstring to match.
    """
    acct, _ = adapt(load("checking"))
    assert set(acct) == set(PERSISTED_ACCOUNT_FIELDS) | set(VALIDATION_ONLY_ACCOUNT_FIELDS)
    assert not set(PERSISTED_ACCOUNT_FIELDS) & set(VALIDATION_ONLY_ACCOUNT_FIELDS)


def test_printed_header_totals_are_carried_verbatim():
    raw = load("checking")
    acct, _ = adapt(raw)
    assert acct["total_deposits"] == raw["total_deposits"]
    assert acct["total_withdrawals"] == raw["total_withdrawals"]
    assert acct["totals_source"] == TOTALS_STATEMENT


def test_header_totals_are_derived_when_not_printed():
    raw = load("checking")
    printed_deposits = raw["total_deposits"]
    printed_withdrawals = raw["total_withdrawals"]
    raw = {**raw, "total_deposits": None, "total_withdrawals": None}

    acct, _ = adapt(raw)

    assert acct["totals_source"] == TOTALS_DERIVED
    # The fixture is clean, so what we derive matches what it printed.
    assert acct["total_deposits"] == pytest.approx(printed_deposits, abs=0.01)
    assert acct["total_withdrawals"] == pytest.approx(printed_withdrawals, abs=0.01)


def test_one_missing_total_derives_both():
    """A half-printed pair would make check B partly circular, which is
    harder to reason about than either extreme. Derive the whole pair."""
    raw = {**load("checking"), "total_withdrawals": None}
    acct, _ = adapt(raw)
    assert acct["totals_source"] == TOTALS_DERIVED
    assert acct["total_deposits"] is not None


def test_derived_totals_are_gross_not_net():
    """A statement prints column sums. A row with both columns filled lands in
    both of them, where its signed `amount` would only carry the net."""
    acct, txns = adapt({"account_number": "1234", "transactions": [
        {"date": "2026-08-01", "description": "split row",
         "withdrawal": 100.00, "deposit": 30.00},
    ]})

    assert txns[0]["amount"] == -70.00
    assert acct["total_deposits"] == 30.00
    assert acct["total_withdrawals"] == 100.00


def test_derived_totals_exclude_skipped_rows():
    acct, txns = adapt({"account_number": "1234", "transactions": [
        {"date": "2026-08-01", "description": "kept", "deposit": 50.00},
        {"date": "not a date", "description": "dropped", "deposit": 999.00},
    ]})

    assert len(txns) == 1
    assert acct["total_deposits"] == 50.00


def test_derived_totals_on_an_empty_statement_are_zero():
    acct, _ = adapt({"account_number": "1234", "transactions": []})
    assert acct["total_deposits"] == 0.0
    assert acct["total_withdrawals"] == 0.0
    assert acct["totals_source"] == TOTALS_DERIVED


def test_derived_difference_is_always_the_sum_of_amounts():
    """The identity that makes a derived check B circular. Pinned here so the
    reason validate.py skips it stays visible."""
    raw = {**load("credit"), "total_deposits": None, "total_withdrawals": None}
    acct, txns = adapt(raw)

    assert acct["total_deposits"] - acct["total_withdrawals"] == pytest.approx(
        sum(t["amount"] for t in txns), abs=0.01)


def test_validation_only_fields_are_not_in_the_api_model():
    """They must never reach the frontend."""
    from models import Account

    for field in VALIDATION_ONLY_ACCOUNT_FIELDS:
        assert field not in Account.model_fields


def test_persisted_fields_cover_the_api_model():
    """Every field the API exposes must be something we actually keep."""
    from models import Account

    api_only = {"transaction_count"}  # derived at Stage 9, not stored per-account
    assert set(Account.model_fields) - api_only <= set(PERSISTED_ACCOUNT_FIELDS)


# --- passthrough fields ----------------------------------------------------

def test_reference_and_balance_carried_through():
    _, txns = adapt(one(reference="9685", balance=472.61, withdrawal=1.50))
    assert txns[0]["reference"] == "9685"
    assert txns[0]["balance"] == 472.61


def test_currency_defaults_to_usd():
    acct, _ = adapt({"account_number": "1234", "currency": None, "transactions": []})
    assert acct["currency"] == "USD"
    acct, _ = adapt({"account_number": "1234", "currency": "CAD", "transactions": []})
    assert acct["currency"] == "CAD"


def test_apr_and_credit_limit_come_from_arguments():
    raw = load("credit")
    assert "apr" not in raw and "credit_limit" not in raw
    acct, _ = adapt(raw, apr=24.99, credit_limit=5000.0)
    assert acct["apr"] == 24.99
    assert acct["credit_limit"] == 5000.0

    acct, _ = adapt(raw)
    assert acct["apr"] is None and acct["credit_limit"] is None


# --- error handling --------------------------------------------------------

def test_broken_fixture_raises_value_error():
    with pytest.raises(ValueError, match="transactions"):
        adapt(load("broken"))


def test_transactions_must_be_a_list():
    with pytest.raises(ValueError):
        adapt({"transactions": "not a list"})


# --- the real fixtures -----------------------------------------------------

def test_checking_fixture_adapts():
    acct, txns = adapt(load("checking"))
    assert acct["account_type"] == "checking"
    assert len(txns) == 30
    assert all(t["account_id"] == acct["id"] for t in txns)


def test_credit_fixture_matches_spec_manual_check():
    """The manual assertions from CLAUDE.md Stage 1."""
    acct, txns = adapt(load("credit"), apr=24.99)
    assert acct["account_type"] == "credit"
    assert "4821" in acct["account_last4"]
    assert all(t["amount"] < 0 for t in txns if t["description"].startswith("SQ"))
    assert any(t["amount"] > 0 for t in txns), "credit fixture needs a payment"


# --- dates stranded in the description ------------------------------------

from adapter import recover_date_from_description  # noqa: E402


def statement(rows, start="2022-06-01", end="2022-06-30"):
    return {"bank_name": "Finance Bank", "account_number": "0000000098765",
            "account_type": "Checking", "statement_period_start": start,
            "statement_period_end": end, "transactions": rows}


def undated(description, **extra):
    return {"date": None, "description": description, "reference": None,
            "withdrawal": 10.0, "deposit": None, "balance": None, **extra}


def test_a_leading_date_in_the_description_is_recovered():
    """Gemini returns date:null and keeps '06/01' inside the description when
    a statement does not visually separate the columns. The parser's own
    to_iso_date cannot help -- every pattern it knows requires a year."""
    _, txns = adapt(statement([undated("06/01 Rent Bill")]))

    assert len(txns) == 1
    assert txns[0]["date"] == date(2022, 6, 1)
    assert txns[0]["description"] == "06/01 Rent Bill", "description stays verbatim"


def test_recovery_never_overrides_a_date_the_parser_did_read():
    _, txns = adapt(statement([
        {"date": "2022-06-15", "description": "06/01 Rent Bill",
         "withdrawal": 10.0, "deposit": None, "balance": None, "reference": None}]))
    assert txns[0]["date"] == date(2022, 6, 15)


def test_a_recovered_date_must_fall_inside_the_statement_period():
    """Without this, a reference number reads as a date.

    The rejected rows are no longer dropped -- the statement-start fallback
    catches them -- but the point stands: neither is given the bogus date it
    appeared to contain.
    """
    _, txns = adapt(statement([undated("12/34 Payment"),        # impossible day
                               undated("09/15 Out of period"),  # real date, wrong month
                               undated("06/08 Electric Bill")]))

    assert [t["date"] for t in txns] == [
        date(2022, 6, 1),    # fallback, NOT 12/34
        date(2022, 6, 1),    # fallback, NOT 2022-09-15
        date(2022, 6, 8),    # genuinely recovered
    ]


def test_a_period_straddling_a_year_end_resolves_both_sides():
    _, txns = adapt(statement([undated("12/20 December charge"),
                               undated("01/05 January charge")],
                              start="2025-12-15", end="2026-01-14"))
    assert [t["date"] for t in txns] == [date(2025, 12, 20), date(2026, 1, 5)]


@pytest.mark.parametrize("description", [
    "Rent Bill", "Payment 06/01", "ref 9685 06/01", "", "06/01Rent", None, 12345,
])
def test_only_a_leading_date_token_counts(description):
    assert recover_date_from_description(description, "2022-06-01", "2022-06-30") is None


def test_recovery_needs_a_statement_period():
    assert recover_date_from_description("06/01 Rent", None, None) is None


def test_the_real_parser_file_recovers_every_row():
    from pathlib import Path
    import json as _json
    path = Path(__file__).resolve().parent.parent / "Bank_Statement_transaction_data_normalized.json"
    if not path.exists():
        pytest.skip("parser output not present")

    raw = _json.loads(path.read_text())
    assert all(r["date"] is None for r in raw["transactions"]), "fixture premise"

    _, txns = adapt(raw)
    assert len(txns) == len(raw["transactions"]) == 9
    assert all(t["date"] is not None for t in txns)


# --- apr and credit_limit belong to borrowing accounts only ---------------

@pytest.mark.parametrize("account_type", [
    "Chequing", "Checking", "Savings", "Money Market", "Visa Debit", "Gibberish",
])
def test_a_non_credit_account_never_carries_an_apr(account_type):
    """A chequing account has no APR. One typed into the upload form against
    one is a mistake, not data, and must not reach accounts[].apr."""
    acct, _ = adapt({"bank_name": "B", "account_number": "1234",
                     "account_type": account_type, "transactions": []},
                    apr=24.99, credit_limit=5000.0)

    assert acct["account_type"] != "credit"
    assert acct["apr"] is None
    assert acct["credit_limit"] is None


@pytest.mark.parametrize("account_type", [
    "Credit Card", "Visa", "Visa Signature", "Mastercard", "Line of Credit",
])
def test_a_credit_account_keeps_its_apr(account_type):
    acct, _ = adapt({"bank_name": "B", "account_number": "1234",
                     "account_type": account_type, "transactions": []},
                    apr=24.99, credit_limit=5000.0)

    assert acct["account_type"] == "credit"
    assert acct["apr"] == 24.99
    assert acct["credit_limit"] == 5000.0


def test_dropping_an_apr_does_not_disturb_anything_else():
    raw = load("checking")
    plain, plain_txns = adapt(raw)
    with_apr, with_txns = adapt(raw, apr=24.99, credit_limit=5000.0)

    assert plain == with_apr
    assert plain_txns == with_txns


# --- TEMPORARY: statement-start fallback for undated rows ------------------
# Remove this block, and the fallback in adapt(), once the parser populates
# `date`. The dates it produces are fabricated.

def test_an_undated_row_falls_back_to_the_statement_start():
    _, txns = adapt(statement([undated("No date anywhere")]))
    assert len(txns) == 1
    assert txns[0]["date"] == date(2022, 6, 1)


def test_the_fallback_is_the_last_resort_not_the_first():
    """Order matters: a real date wins, then one recovered from the
    description, and only then the statement start."""
    _, txns = adapt(statement([
        {"date": "2022-06-15", "description": "anything", "withdrawal": 1.0,
         "deposit": None, "balance": None, "reference": None},
        undated("06/08 Electric Bill"),
        undated("No date anywhere"),
    ]))
    assert [t["date"] for t in txns] == [
        date(2022, 6, 15),   # the parser's own date
        date(2022, 6, 8),    # recovered from the description
        date(2022, 6, 1),    # statement start
    ]


def test_without_a_statement_period_the_row_is_still_dropped():
    """The fallback needs something to fall back to."""
    _, txns = adapt({"bank_name": "B", "account_number": "1234",
                     "statement_period_start": None,
                     "transactions": [undated("No date anywhere")]})
    assert txns == []


def test_every_fallback_row_lands_on_the_same_day():
    """Which is why spending_over_time is unreliable for them -- they all
    collapse into one bucket."""
    _, txns = adapt(statement([undated(f"Row {i}") for i in range(5)]))
    assert len({t["date"] for t in txns}) == 1


def test_identical_fallback_dates_do_not_fabricate_a_subscription():
    """The one thing that could have gone badly. Same merchant, same
    fabricated date, five times -- zero-length gaps, which match_cadence
    rejects, so no invented subscription reaches the dashboard."""
    from analyze import find_recurring

    _, txns = adapt(statement([undated("Netflix") for _ in range(5)]))
    for txn in txns:
        txn["merchant"] = "Netflix"
        txn["category"] = "Subscriptions"

    assert find_recurring(txns) == []
