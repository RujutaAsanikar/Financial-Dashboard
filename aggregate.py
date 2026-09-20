"""Assemble the dashboard payload from DuckDB. Stage 9.

Everything the earlier stages computed converges here into the exact shape
models.py froze at Stage 0.

The aggregates are SQL rather than pandas. GROUP BY and SUM over whole
columns is the workload DuckDB was chosen for, and pushing them into the
engine keeps this module about shaping the response rather than about
arithmetic.

TWO RULES THAT DECIDE MOST OF THIS FILE
---------------------------------------
Transfers are excluded from every spending figure. A card payment is money
moving between accounts the user owns, so counting it inflates spending and
income by the same amount. It is reported separately in transfers_excluded so
the exclusion is visible rather than silent.

Amounts flip sign at this boundary and nowhere else. Internally spending is
negative; the API reports total_spent and every by_category amount as
positive numbers. summary.net is the only field that may legitimately be
negative.
"""

import logging
from collections import Counter

import analyze
import db

logger = logging.getLogger(__name__)

# Spending figures ignore transfers. Written once here and reused so the
# three aggregates cannot drift apart.
SPEND_FILTER = "WHERE NOT COALESCE(is_transfer, FALSE)"


def _summary(con) -> dict:
    row = con.execute(f"""
        SELECT COALESCE(ABS(SUM(CASE WHEN amount < 0 THEN amount END)), 0),
               COALESCE(SUM(CASE WHEN amount > 0 THEN amount END), 0),
               COUNT(*),
               MIN(date), MAX(date)
        FROM transactions {SPEND_FILTER}
    """).fetchone()

    spent, income, count, first, last = row
    return {
        "total_spent": round(float(spent), 2),
        "total_income": round(float(income), 2),
        "net": round(float(income) - float(spent), 2),
        "transaction_count": int(count),
        # Empty strings rather than null: the field is typed str, and a
        # dashboard with no data still has to validate.
        "period_start": first.isoformat() if first else "",
        "period_end": last.isoformat() if last else "",
    }


def _by_category(con) -> list[dict]:
    rows = con.execute(f"""
        SELECT COALESCE(NULLIF(category, ''), 'Other') AS category,
               ABS(SUM(amount)) AS amount,
               COUNT(*)          AS count
        FROM transactions {SPEND_FILTER} AND amount < 0
        GROUP BY 1
        ORDER BY amount DESC, category
    """).fetchall()
    return [{"category": c, "amount": round(float(a), 2), "count": int(n)}
            for c, a, n in rows]


def _spending_over_time(con) -> list[dict]:
    rows = con.execute(f"""
        SELECT strftime(date, '%Y-%m') AS month,
               ABS(SUM(amount))        AS amount
        FROM transactions {SPEND_FILTER} AND amount < 0
        GROUP BY 1
        ORDER BY 1
    """).fetchall()
    return [{"month": m, "amount": round(float(a), 2)} for m, a in rows]


def _accounts(con) -> list[dict]:
    rows = con.execute("""
        SELECT a.id, a.bank_name, a.account_holder_name, a.account_last4,
               a.account_type, a.closing_balance, a.apr,
               COUNT(t.id) AS transaction_count
        FROM accounts a
        LEFT JOIN transactions t ON t.account_id = a.id
        GROUP BY ALL
        ORDER BY a.id
    """).fetchall()
    return [{"id": r[0], "bank_name": r[1], "account_holder_name": r[2],
             "account_last4": r[3], "account_type": r[4],
             "closing_balance": r[5], "apr": r[6],
             "transaction_count": int(r[7])} for r in rows]


def _transfers_excluded(con) -> dict:
    count, total = con.execute("""
        SELECT COUNT(*), COALESCE(SUM(ABS(amount)), 0)
        FROM transactions WHERE COALESCE(is_transfer, FALSE)
    """).fetchone()
    # Both halves of a pair are counted, so the total is twice the money
    # that moved. That is the honest figure for "rows excluded".
    return {"count": int(count), "total": round(float(total), 2)}


def _payoff(accounts: list[dict]) -> list[dict]:
    """One entry per credit account with an APR. Empty otherwise.

    A balance of zero or a missing APR produces nothing rather than an entry
    with an empty scenario list -- the frontend's empty state is a better
    answer than a card with no chart in it.
    """
    payoff = []
    for account in accounts:
        if account["account_type"] != "credit" or account["apr"] is None:
            continue
        balance = account["closing_balance"]
        if balance is None or balance <= 0:
            continue
        scenarios = analyze.payoff_scenarios(balance, account["apr"])
        if not scenarios:
            logger.info("No payoff scenario amortizes for %s", account["id"])
            continue
        payoff.append({"account_id": account["id"], "balance": round(balance, 2),
                       "apr": account["apr"], "scenarios": scenarios})
    return payoff


def _recurring(txns: list[dict]) -> tuple[list[dict], dict, list[dict], dict]:
    """Subscriptions and repeated spending, each with its totals.

    Recomputed on read rather than persisted: find_recurring needs whole
    rows, not the stored is_recurring boolean, and recomputing means the
    lists are never stale after a new upload.
    """
    found = analyze.find_recurring(txns)
    subscriptions, repeated = analyze.split_recurring(found)

    def trim(rows, keep):
        return [{k: v for k, v in row.items() if k in keep} for row in rows]

    subscription_keys = {"merchant", "amount", "cadence_days", "annual_cost",
                         "price_change_pct", "first_seen", "next_expected", "account_id"}
    repeated_keys = {"merchant", "amount", "cadence_days", "annual_cost",
                     "occurrences", "first_seen", "last_seen", "account_id"}

    return (
        trim(subscriptions, subscription_keys),
        {"count": len(subscriptions),
         "annual_cost": round(sum(s["annual_cost"] for s in subscriptions), 2),
         "price_increases": sum(1 for s in subscriptions
                                if (s.get("price_change_pct") or 0) > 0)},
        trim(repeated, repeated_keys),
        {"count": len(repeated),
         "annual_cost": round(sum(r["annual_cost"] for r in repeated), 2)},
    )


def build_dashboard() -> dict:
    """The whole payload, in the exact shape models.DashboardResponse freezes."""
    db.init_db()

    with db.get_con(read_only=True) as con:
        summary = _summary(con)
        by_category = _by_category(con)
        over_time = _spending_over_time(con)
        accounts = _accounts(con)
        transfers = _transfers_excluded(con)

    txns = db.all_transactions()
    subscriptions, subscription_totals, repeated, repeated_totals = _recurring(txns)

    dashboard = {
        "accounts": accounts,
        "summary": summary,
        "by_category": by_category,
        "spending_over_time": over_time,
        "subscriptions": subscriptions,
        "subscription_totals": subscription_totals,
        "repeated_spending": repeated,
        "repeated_spending_totals": repeated_totals,
        "payoff": _payoff(accounts),
        "transfers_excluded": transfers,
        "extraction": db.extraction_summary(),
    }

    logger.info(
        "Dashboard: %d account(s), %d transaction(s), %d category(ies), "
        "%d subscription(s), %d repeated, %d payoff, reconciled=%s",
        len(accounts), summary["transaction_count"], len(by_category),
        len(subscriptions), len(repeated), len(dashboard["payoff"]),
        dashboard["extraction"]["reconciled"],
    )
    return dashboard


def ingest(parser_json: dict, apr: float | None = None,
           account_nickname: str | None = None) -> dict:
    """Run one statement through the pipeline and store the results.

    CLAUDE.md's order, with one thing it does not spell out: transfer and
    recurring detection run over EVERYTHING in the database, not just this
    statement. A card payment pairs with a withdrawal that may have arrived
    in a different upload, and a subscription's history spans statements.
    """
    from adapter import adapt
    from categorize import categorize
    from normalize import resolve
    from transfers import mark_transfers
    from validate import reconcile

    account, txns = adapt(parser_json, apr=apr)
    if account_nickname:
        account["bank_name"] = account_nickname

    result = reconcile(account, txns)

    for txn in txns:
        txn["merchant"] = resolve(txn["description"])
    categorize(txns)

    db.init_db()
    db.upsert_account(account)
    inserted = db.insert_transactions(txns)
    db.record_extraction(account["id"], result)

    # Detection sees the whole database, then the flags are written back.
    stored = db.all_transactions()
    mark_transfers(stored)
    analyze.find_recurring(stored)
    db.update_flags(stored)

    logger.info("Ingested %s: %d new row(s) of %d, reconciled=%s",
                account["id"], inserted, len(txns), result["reconciled"])
    return {"account_id": account["id"], "inserted": inserted,
            "submitted": len(txns), "reconciliation": result}
