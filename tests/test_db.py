"""Stage 3 persistence and dedupe tests. See CLAUDE.md section 5, Stage 3.

The dedupe tests are the important ones. Without them, uploading August and
then September double-counts every overlapping day and every headline number
on the dashboard is wrong but plausible.
"""

import copy
import json
from datetime import date
from pathlib import Path

import pytest

import db
from adapter import PERSISTED_ACCOUNT_FIELDS, adapt

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Every test gets its own database file. Never touches finance.duckdb."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.duckdb")
    db.init_db()
    yield


def load(name: str):
    return adapt(json.loads((FIXTURES / f"{name}.json").read_text()))


def count(table: str) -> int:
    with db.get_con() as con:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# --- CLAUDE.md's three required cases --------------------------------------

def test_inserting_thirty_rows_returns_thirty():
    _, txns = load("checking")
    assert len(txns) == 30
    assert db.insert_transactions(txns) == 30
    assert count("transactions") == 30


def test_reinserting_the_same_rows_returns_zero():
    """Re-uploading a statement must change nothing."""
    _, txns = load("checking")
    db.insert_transactions(txns)

    _, again = load("checking")
    assert db.insert_transactions(again) == 0
    assert count("transactions") == 30


def test_one_changed_description_inserts_exactly_one_row():
    _, txns = load("checking")
    db.insert_transactions(txns)

    _, modified = load("checking")
    modified[9]["description"] = "SOMETHING COMPLETELY DIFFERENT"

    assert db.insert_transactions(modified) == 1
    assert count("transactions") == 31


# --- id determinism --------------------------------------------------------

def test_txn_id_is_deterministic_and_sixteen_hex_chars():
    args = ("chase-4821", date(2026, 8, 18), "SQ *COFFEE", -6.25, None)
    first = db.txn_id(*args)

    assert first == db.txn_id(*args)
    assert len(first) == 16
    assert all(c in "0123456789abcdef" for c in first)


@pytest.mark.parametrize("field,value", [
    ("account_id", "other-1111"),
    ("date", date(2026, 8, 19)),
    ("description", "SQ *TEA"),
    ("amount", -6.26),
    ("reference", "9685"),
])
def test_every_hash_input_changes_the_id(field, value):
    base = dict(account_id="chase-4821", date=date(2026, 8, 18),
                description="SQ *COFFEE", amount=-6.25, reference=None)
    assert db.txn_id(**base) != db.txn_id(**{**base, field: value})


def test_ids_do_not_depend_on_what_is_already_stored():
    """Occurrence numbers come from the statement, never from the database."""
    _, first = load("checking")
    db.assign_txn_ids(first)
    db.insert_transactions(copy.deepcopy(first))

    _, second = load("checking")
    db.assign_txn_ids(second)

    assert [t["id"] for t in first] == [t["id"] for t in second]


# --- the collision that would silently delete a real transaction ----------

def _identical_rows(n: int) -> list[dict]:
    """n transactions indistinguishable by content: two coffees, one day."""
    return [{"account_id": "chase-4821", "date": date(2026, 8, 18),
             "description": "SQ *COFFEE TREE ROASTER", "amount": -6.25,
             "balance": None, "reference": None, "merchant": None,
             "category": None, "confidence": None,
             "is_transfer": False, "is_recurring": False} for _ in range(n)]


def test_genuinely_identical_rows_both_survive():
    """Two $6.25 coffees on one day is a real thing that happens.

    Hashing content alone gives them one id and the second is dropped as a
    duplicate -- a real transaction quietly deleted. The occurrence counter is
    what prevents it.
    """
    rows = _identical_rows(2)

    assert db.insert_transactions(rows) == 2
    assert count("transactions") == 2
    assert rows[0]["id"] != rows[1]["id"]


def test_three_identical_rows_all_survive():
    assert db.insert_transactions(_identical_rows(3)) == 3


def test_identical_rows_still_dedupe_on_re_upload():
    """The hard part: distinguishable within a statement, identical across
    uploads of it. Both properties at once, or the feature is useless."""
    db.insert_transactions(_identical_rows(2))

    assert db.insert_transactions(_identical_rows(2)) == 0
    assert count("transactions") == 2


def test_a_third_coffee_appearing_later_is_new():
    db.insert_transactions(_identical_rows(2))

    assert db.insert_transactions(_identical_rows(3)) == 1
    assert count("transactions") == 3


# --- overlapping statements, the reason dedupe exists ---------------------

def test_overlapping_statements_do_not_double_count():
    _, txns = load("checking")
    august, overlap = txns[:20], txns[10:]

    assert db.insert_transactions(august) == 20
    assert db.insert_transactions(overlap) == 10
    assert count("transactions") == 30, "the ten shared rows must land once"


def test_stored_rows_are_not_overwritten_by_a_re_upload():
    """A stored row may have been categorized or flagged since. ON CONFLICT
    DO NOTHING protects that work; DO UPDATE would discard it."""
    _, txns = load("checking")
    db.insert_transactions(txns)

    with db.get_con() as con:
        con.execute("UPDATE transactions SET category = 'Groceries', is_recurring = TRUE")

    _, again = load("checking")
    db.insert_transactions(again)

    with db.get_con() as con:
        rows = con.execute(
            "SELECT COUNT(*) FROM transactions WHERE category = 'Groceries'").fetchone()[0]
    assert rows == 30, "re-upload wiped enrichment from later stages"


# --- accounts --------------------------------------------------------------

def test_upsert_account_inserts_then_updates():
    acct, _ = load("checking")
    db.upsert_account(acct)
    assert count("accounts") == 1

    db.upsert_account({**acct, "closing_balance": 9999.99})
    assert count("accounts") == 1

    with db.get_con() as con:
        stored = con.execute(
            "SELECT closing_balance FROM accounts WHERE id = ?", [acct["id"]]).fetchone()[0]
    assert stored == 9999.99


def test_upsert_does_not_erase_apr_with_a_null():
    """apr comes from the upload form. A second upload with the form left
    blank must not empty the payoff section."""
    acct, _ = load("credit")
    db.upsert_account({**acct, "apr": 24.99, "credit_limit": 5000.0})
    db.upsert_account({**acct, "apr": None, "credit_limit": None})

    with db.get_con() as con:
        apr, limit = con.execute(
            "SELECT apr, credit_limit FROM accounts WHERE id = ?", [acct["id"]]).fetchone()
    assert apr == 24.99
    assert limit == 5000.0


def test_validation_only_fields_are_not_columns():
    """PERSISTED_ACCOUNT_FIELDS is what keeps the header totals out of here."""
    acct, _ = load("checking")
    assert "total_deposits" in acct, "the dict carries it"

    db.upsert_account(acct)
    with db.get_con() as con:
        columns = {row[1] for row in con.execute("PRAGMA table_info(accounts)").fetchall()}

    assert columns == set(PERSISTED_ACCOUNT_FIELDS)
    assert "total_deposits" not in columns
    assert "totals_source" not in columns


def test_account_last4_is_all_that_reaches_the_database():
    raw = json.loads((FIXTURES / "checking.json").read_text())
    acct, txns = adapt(raw)
    db.upsert_account(acct)
    db.insert_transactions(txns)

    with db.get_con() as con:
        dumped = str(con.execute("SELECT * FROM accounts").fetchall())
        dumped += str(con.execute("SELECT * FROM transactions").fetchall())

    assert raw["account_number"] not in dumped
    assert "000123456789" not in dumped


# --- schema plumbing -------------------------------------------------------

def test_init_db_is_idempotent():
    _, txns = load("checking")
    db.insert_transactions(txns)

    db.init_db()
    db.init_db()

    assert count("transactions") == 30, "re-initializing must not drop data"


def test_reset_db_empties_every_table():
    acct, txns = load("checking")
    db.upsert_account(acct)
    db.insert_transactions(txns)
    with db.get_con() as con:
        con.execute("INSERT INTO merchant_cache VALUES ('Netflix', 'Subscriptions')")

    db.reset_db()

    for table in ("transactions", "accounts", "merchant_cache"):
        assert count(table) == 0


def test_read_only_connection_rejects_writes():
    """The Stage 10 guardrail, enforced by the engine rather than by us."""
    _, txns = load("checking")
    db.insert_transactions(txns)

    with db.get_con(read_only=True) as con:
        assert con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 30
        with pytest.raises(Exception):
            con.execute("DELETE FROM transactions")


def test_read_only_on_a_missing_database_initializes_instead_of_failing():
    db.DB_PATH.unlink(missing_ok=True)
    with db.get_con(read_only=True) as con:
        assert con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0


# --- types and edges -------------------------------------------------------

def test_empty_insert_is_a_no_op():
    assert db.insert_transactions([]) == 0


def test_nulls_are_stored_as_null_not_as_nan():
    """pandas would turn None into NaN in a DOUBLE column. Parameter binding
    is used precisely to avoid that."""
    _, txns = load("checking")
    txns[0]["balance"] = None
    txns[0]["confidence"] = None
    db.insert_transactions(txns)

    with db.get_con() as con:
        nulls = con.execute(
            "SELECT COUNT(*) FROM transactions WHERE balance IS NULL").fetchone()[0]
    assert nulls == 1


def test_dates_round_trip_as_dates():
    _, txns = load("checking")
    db.insert_transactions(txns)

    with db.get_con() as con:
        stored = con.execute("SELECT MIN(date) FROM transactions").fetchone()[0]

    assert isinstance(stored, date)
    assert stored == min(t["date"] for t in txns)


def test_both_fixtures_coexist():
    for name in ("checking", "credit"):
        acct, txns = load(name)
        db.upsert_account(acct)
        db.insert_transactions(txns)

    assert count("accounts") == 2
    assert count("transactions") == 60
