"""Stage 9 tests: dashboard assembly and the API. CLAUDE.md section 5, Stage 9.

This is the GO/NO-GO gate, so the emphasis is on the things that would make
the dashboard quietly wrong rather than obviously broken: signs flipped the
wrong way, transfers double-counting, and a reconciliation badge that says
verified when nothing was verified.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import aggregate
import db
import main
import normalize as N
from models import DashboardResponse

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Own database per test, and no network from any stage."""
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.duckdb")
    monkeypatch.setattr(N, "_aliases", None)
    monkeypatch.delenv(N.LIVE_LOOKUP_ENV, raising=False)
    monkeypatch.delenv(aggregate.analyze.LIVE_CLASSIFY_ENV, raising=False)
    monkeypatch.setattr(
        N.triq, "enrich",
        lambda *a, **k: pytest.fail("a test attempted a live Triqai call"))
    monkeypatch.setattr(
        aggregate.analyze, "classify_live",
        lambda *a, **k: pytest.fail("a test attempted a live Claude call"))
    db.reset_db()
    yield


@pytest.fixture
def client():
    return TestClient(main.app)


def load(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def both_fixtures():
    aggregate.ingest(load("checking"))
    aggregate.ingest(load("credit"), apr=24.99)


# --- the empty case, which the demo hits after every reset ----------------

def test_an_empty_database_still_produces_a_valid_dashboard():
    dashboard = aggregate.build_dashboard()
    DashboardResponse(**dashboard)

    assert dashboard["accounts"] == []
    assert dashboard["summary"]["transaction_count"] == 0
    assert dashboard["summary"]["total_spent"] == 0
    assert dashboard["payoff"] == []


def test_an_empty_database_is_not_reported_as_reconciled():
    """Nothing was checked, so the badge must not be green. Same rule
    validate.py applies per statement, applied across statements."""
    assert aggregate.build_dashboard()["extraction"]["reconciled"] is False


# --- sign conventions ------------------------------------------------------

def test_total_spent_is_positive_and_net_is_income_minus_spent():
    both_fixtures()
    summary = aggregate.build_dashboard()["summary"]

    assert summary["total_spent"] > 0
    assert summary["total_income"] > 0
    assert summary["net"] == pytest.approx(
        summary["total_income"] - summary["total_spent"], abs=0.01)


def test_by_category_amounts_are_positive_and_sorted_descending():
    both_fixtures()
    rows = aggregate.build_dashboard()["by_category"]

    amounts = [r["amount"] for r in rows]
    assert all(a >= 0 for a in amounts)
    assert amounts == sorted(amounts, reverse=True)


def test_by_category_sums_to_total_spent():
    """If these disagree, one of them is computing the wrong set of rows."""
    both_fixtures()
    dashboard = aggregate.build_dashboard()

    assert sum(r["amount"] for r in dashboard["by_category"]) == pytest.approx(
        dashboard["summary"]["total_spent"], abs=0.01)


def test_spending_over_time_is_ascending_and_sums_to_total_spent():
    both_fixtures()
    dashboard = aggregate.build_dashboard()
    months = dashboard["spending_over_time"]

    assert [m["month"] for m in months] == sorted(m["month"] for m in months)
    assert all(len(m["month"]) == 7 and m["month"][4] == "-" for m in months)
    assert sum(m["amount"] for m in months) == pytest.approx(
        dashboard["summary"]["total_spent"], abs=0.01)


# --- transfers -------------------------------------------------------------

def test_transfers_are_excluded_from_every_spending_figure():
    both_fixtures()
    dashboard = aggregate.build_dashboard()

    with db.get_con(read_only=True) as con:
        stored = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]

    excluded = dashboard["transfers_excluded"]["count"]
    assert excluded > 0, "the fixtures contain a planted transfer pair"
    assert dashboard["summary"]["transaction_count"] + excluded == stored


def test_excluding_transfers_does_not_change_net():
    """The invariant from Stage 6, now asserted end to end. A transfer nets
    to zero, so dropping both halves moves spending and income equally."""
    both_fixtures()
    dashboard = aggregate.build_dashboard()

    with db.get_con(read_only=True) as con:
        raw_net = con.execute("SELECT SUM(amount) FROM transactions").fetchone()[0]

    assert dashboard["summary"]["net"] == pytest.approx(raw_net, abs=0.01)


def test_no_transfer_row_appears_in_a_category_total():
    both_fixtures()
    dashboard = aggregate.build_dashboard()

    with db.get_con(read_only=True) as con:
        transferred = con.execute(
            "SELECT COALESCE(SUM(ABS(amount)), 0) FROM transactions "
            "WHERE is_transfer AND amount < 0").fetchone()[0]

    assert sum(r["amount"] for r in dashboard["by_category"]) == pytest.approx(
        dashboard["summary"]["total_spent"], abs=0.01)
    assert transferred > 0
    assert dashboard["summary"]["total_spent"] < transferred + sum(
        r["amount"] for r in dashboard["by_category"])


# --- payoff ----------------------------------------------------------------

def test_payoff_only_for_credit_accounts_with_an_apr():
    both_fixtures()
    payoff = aggregate.build_dashboard()["payoff"]

    assert len(payoff) == 1
    assert payoff[0]["account_id"] == "chase-4821"
    assert len(payoff[0]["scenarios"]) == 4
    assert all(s["label"] for s in payoff[0]["scenarios"])


def test_no_payoff_without_an_apr():
    aggregate.ingest(load("credit"))          # no apr passed
    assert aggregate.build_dashboard()["payoff"] == []


def test_no_payoff_for_a_checking_account():
    aggregate.ingest(load("checking"), apr=9.99)
    assert aggregate.build_dashboard()["payoff"] == []


# --- extraction ------------------------------------------------------------

def test_extraction_reflects_the_stored_verdicts():
    both_fixtures()
    extraction = aggregate.build_dashboard()["extraction"]

    assert extraction["reconciled"] is True
    assert extraction["delta"] == 0.0
    assert extraction["rows_needing_review"] == 0


def test_one_bad_statement_makes_the_whole_badge_false():
    aggregate.ingest(load("checking"))
    tampered = load("credit")
    tampered["transactions"][4]["withdrawal"] = 9999.99
    aggregate.ingest(tampered, apr=24.99)

    assert aggregate.build_dashboard()["extraction"]["reconciled"] is False


def test_reconciliation_survives_a_second_statement_for_one_account():
    """The reason extraction is stored rather than recomputed.

    Re-deriving it from the accounts table would compare transactions
    spanning both statements against the second statement's opening and
    closing balances, and report a correct extraction as failed.
    """
    first = load("credit")
    second = load("credit")
    second["statement_period_start"] = "2026-09-17"
    second["statement_period_end"] = "2026-10-16"
    second["opening_balance"] = 3583.83
    second["closing_balance"] = 3583.83 - 100.0
    second["total_withdrawals"] = 0.0
    second["total_deposits"] = 100.0
    second["transactions"] = [{"date": "2026-09-20", "description": "PAYMENT",
                               "withdrawal": None, "deposit": 100.0,
                               "balance": 3483.83, "reference": None}]

    aggregate.ingest(first, apr=24.99)
    aggregate.ingest(second, apr=24.99)

    assert aggregate.build_dashboard()["extraction"]["reconciled"] is True


# --- accounts --------------------------------------------------------------

def test_account_transaction_counts_include_transfers():
    """accounts[].transaction_count is 'rows on this account', not 'rows we
    counted as spending' -- the two differ and the distinction matters."""
    both_fixtures()
    dashboard = aggregate.build_dashboard()

    assert sum(a["transaction_count"] for a in dashboard["accounts"]) == 60
    assert dashboard["summary"]["transaction_count"] == 58


def test_no_full_account_number_anywhere_in_the_payload():
    """CLAUDE.md's definition of done, checked at the API boundary."""
    raw = load("checking")
    aggregate.ingest(raw)

    serialized = json.dumps(aggregate.build_dashboard(), default=str)
    assert raw["account_number"] not in serialized
    assert "000123456789" not in serialized


# --- ingest ----------------------------------------------------------------

def test_re_ingesting_the_same_statement_changes_nothing():
    aggregate.ingest(load("credit"), apr=24.99)
    before = aggregate.build_dashboard()

    result = aggregate.ingest(load("credit"), apr=24.99)
    after = aggregate.build_dashboard()

    assert result["inserted"] == 0
    assert before["summary"] == after["summary"]
    assert before["by_category"] == after["by_category"]


def test_flags_are_written_back_to_the_database():
    both_fixtures()
    with db.get_con(read_only=True) as con:
        flagged = con.execute(
            "SELECT COUNT(*) FROM transactions WHERE is_transfer").fetchone()[0]
    assert flagged == 2


def test_transfer_detection_sees_statements_from_earlier_uploads():
    """The pair spans two files, so detection has to run over the database,
    not over the statement being uploaded."""
    aggregate.ingest(load("checking"))
    assert aggregate.build_dashboard()["transfers_excluded"]["count"] == 0

    aggregate.ingest(load("credit"), apr=24.99)
    assert aggregate.build_dashboard()["transfers_excluded"]["count"] == 2


def test_account_nickname_overrides_the_bank_name():
    aggregate.ingest(load("credit"), apr=24.99, account_nickname="My Travel Card")
    assert aggregate.build_dashboard()["accounts"][0]["bank_name"] == "My Travel Card"


# --- the API ---------------------------------------------------------------

def test_dashboard_endpoint_matches_the_frozen_contract(client):
    both_fixtures()
    response = client.get("/api/dashboard")

    assert response.status_code == 200
    DashboardResponse(**response.json())


def test_upload_returns_the_updated_dashboard(client):
    with (FIXTURES / "credit.json").open("rb") as handle:
        response = client.post("/api/upload",
                               files={"file": ("credit.json", handle, "application/json")},
                               data={"apr": "24.99"})

    assert response.status_code == 200
    body = response.json()
    DashboardResponse(**body)
    assert body["summary"]["transaction_count"] == 30
    assert len(body["payoff"]) == 1


@pytest.mark.parametrize("payload,expected", [
    (b"", 400),                       # empty file
    (b"not json at all", 400),        # unparseable
    (b'{"foo": 1}', 422),             # valid JSON, wrong shape
    (b"[]", 422),                     # valid JSON, not an object
])
def test_upload_rejects_bad_input_with_a_clean_error(client, payload, expected):
    response = client.post("/api/upload",
                           files={"file": ("x.json", payload, "application/json")})
    assert response.status_code == expected
    assert "detail" in response.json(), "must be a JSON error, not a traceback"


def test_upload_rejects_an_oversized_file(client, monkeypatch):
    monkeypatch.setattr(main, "MAX_UPLOAD_BYTES", 10)
    response = client.post("/api/upload",
                           files={"file": ("big.json", b'{"transactions": []}' * 10,
                                           "application/json")})
    assert response.status_code == 413


def test_transactions_endpoint_filters_and_limits(client):
    both_fixtures()

    everything = client.get("/api/transactions?limit=1000").json()
    assert everything["total"] == 60
    assert everything["count"] == 60

    limited = client.get("/api/transactions?limit=5").json()
    assert limited["count"] == 5
    assert limited["total"] == 60

    by_account = client.get("/api/transactions?account_id=chase-4821").json()
    assert by_account["total"] == 30
    assert all(t["account_id"] == "chase-4821" for t in by_account["transactions"])

    by_category = client.get("/api/transactions?limit=1000&category=Groceries").json()
    assert by_category["total"] > 0
    assert all(t["category"] == "Groceries" for t in by_category["transactions"])


def test_transactions_dates_serialize_as_strings(client):
    both_fixtures()
    rows = client.get("/api/transactions?limit=1").json()["transactions"]
    assert isinstance(rows[0]["date"], str)


@pytest.mark.parametrize("bad", ["limit=0", "limit=99999", "limit=abc"])
def test_transactions_rejects_a_bad_limit(client, bad):
    assert client.get(f"/api/transactions?{bad}").status_code == 422


def test_reset_empties_everything(client):
    both_fixtures()
    assert client.post("/api/reset").status_code == 200

    dashboard = client.get("/api/dashboard").json()
    assert dashboard["summary"]["transaction_count"] == 0
    assert dashboard["accounts"] == []
    assert dashboard["extraction"]["reconciled"] is False


def test_the_stage_zero_mock_is_still_served(client):
    """The frontend was built against it and the database is empty after
    every demo reset."""
    response = client.get("/api/mock-dashboard")
    assert response.status_code == 200
    DashboardResponse(**response.json())
    assert len(response.json()["subscriptions"]) == 11


def test_health_still_works(client):
    assert client.get("/api/health").json() == {"ok": True}
