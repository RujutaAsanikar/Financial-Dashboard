"""DuckDB persistence and deduplication. Stage 3.

The job here is narrow: store canonical rows, and make re-uploading a
statement a no-op. Without that second property, uploading August and then
September double-counts every transaction in the overlapping days, and every
headline figure on the dashboard is wrong in a way that looks plausible.

Deduplication works by giving each row a deterministic id derived from its own
contents, so the same row always lands on the same primary key no matter how
many times it arrives. See txn_id() for the one subtlety that makes this
harder than it sounds.
"""

import hashlib
import logging
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from adapter import PERSISTED_ACCOUNT_FIELDS

logger = logging.getLogger(__name__)

# Module-level so tests can point it at a tmp file. Read at call time, never
# captured, so monkeypatching it actually takes effect.
DB_PATH = Path(os.getenv("FINANCE_DB_PATH", "finance.duckdb"))

TRANSACTION_COLUMNS = (
    "id", "account_id", "date", "description", "merchant", "category",
    "confidence", "amount", "balance", "reference", "is_transfer", "is_recurring",
)

_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS accounts (
        id                  VARCHAR PRIMARY KEY,
        bank_name           VARCHAR,
        account_holder_name VARCHAR,
        account_last4       VARCHAR,
        account_type        VARCHAR,
        currency            VARCHAR,
        opening_balance     DOUBLE,
        closing_balance     DOUBLE,
        apr                 DOUBLE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS transactions (
        id           VARCHAR PRIMARY KEY,
        account_id   VARCHAR,
        date         DATE,
        description  VARCHAR,
        merchant     VARCHAR,
        category     VARCHAR,
        confidence   DOUBLE,
        amount       DOUBLE,
        balance      DOUBLE,
        reference    VARCHAR,
        is_transfer  BOOLEAN,
        is_recurring BOOLEAN
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS merchant_cache (
        merchant VARCHAR PRIMARY KEY,
        category VARCHAR
    )
    """,
    # Added in Stage 9. Reconciliation is a fact about ONE statement, checked
    # against header totals that are deliberately never stored, so it cannot
    # be recomputed later. Re-deriving it from the accounts table would also
    # be wrong: upload August then September for one card and the account row
    # holds September's opening/closing while transactions span both months,
    # so sum(amounts) no longer equals closing - opening and a correct
    # extraction reports as failed.
    """
    CREATE TABLE IF NOT EXISTS extractions (
        id            VARCHAR PRIMARY KEY,
        account_id    VARCHAR,
        uploaded_at   TIMESTAMP,
        reconciled    BOOLEAN,
        delta         DOUBLE,
        checks_run    INTEGER,
        rows_flagged  INTEGER
    )
    """,
)

_TABLES = ("transactions", "accounts", "merchant_cache", "extractions")


def get_con(read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open a connection to the database file.

    read_only=True is the Stage 10 guardrail: the text-to-SQL endpoint runs
    LLM-authored queries, and a read-only connection makes a destructive
    statement impossible at the engine level rather than relying on our
    parser catching it. DuckDB cannot create a file in read-only mode, so an
    absent database is initialized first.
    """
    if read_only and not DB_PATH.exists():
        init_db()
    return duckdb.connect(str(DB_PATH), read_only=read_only)


def init_db() -> None:
    """Create the tables if they do not already exist. Safe to call repeatedly."""
    with duckdb.connect(str(DB_PATH)) as con:
        for statement in _SCHEMA:
            con.execute(statement)


def reset_db() -> None:
    """Drop and recreate every table. For demo resets."""
    with duckdb.connect(str(DB_PATH)) as con:
        for table in _TABLES:
            con.execute(f"DROP TABLE IF EXISTS {table}")
        for statement in _SCHEMA:
            con.execute(statement)
    logger.info("Database reset: %s", DB_PATH)


def txn_id(account_id: str, date, description: str, amount: float,
           reference: str | None, occurrence: int = 0) -> str:
    """Deterministic row id: sha256 of the fields joined by "|", first 16 hex.

    `occurrence` disambiguates rows that are genuinely identical. Two $6.25
    coffees at the same shop on the same day with no reference number are
    indistinguishable by content, so hashing content alone gives them the same
    id and the second one is silently dropped as a duplicate -- a real
    transaction quietly deleted, which is worse than the double-counting this
    whole mechanism exists to prevent.

    The counter is assigned by position within the statement (see
    assign_txn_ids), so it is stable across re-uploads of that statement: the
    first coffee is always occurrence 0 and the second always 1. Dedupe still
    works, and both coffees survive.

    This extends CLAUDE.md's five hash inputs by one field. The alternative
    was accepting silent data loss on a common real-world pattern.
    """
    raw = "|".join([
        str(account_id), str(date), str(description), str(amount),
        str(reference), str(occurrence),
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def assign_txn_ids(txns: list[dict]) -> list[dict]:
    """Give every row an id, in place. Rows that already have one keep it.

    Occurrence numbers are counted over the whole list in order, so they
    depend only on the statement's contents and ordering -- never on what is
    already in the database. That is what keeps re-uploads idempotent.
    """
    seen: Counter = Counter()
    for txn in txns:
        key = (txn.get("account_id"), txn.get("date"), txn.get("description"),
               txn.get("amount"), txn.get("reference"))
        occurrence = seen[key]
        seen[key] += 1
        if not txn.get("id"):
            txn["id"] = txn_id(*key, occurrence=occurrence)
    return txns


def upsert_account(account: dict) -> None:
    """Insert or update one account.

    Columns are named from PERSISTED_ACCOUNT_FIELDS rather than from the
    dict's keys, because the account dict also carries validation-only fields
    (the header totals) that have no columns here.

    On conflict, a new non-null value wins but a null never overwrites a
    stored one. That matters most for apr: it comes from the upload form, so a
    second upload that leaves the form blank would otherwise erase it and
    silently empty the payoff section.
    """
    columns = PERSISTED_ACCOUNT_FIELDS
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(
        f"{c} = COALESCE(excluded.{c}, accounts.{c})" for c in columns if c != "id"
    )
    values = [account.get(c) for c in columns]

    with duckdb.connect(str(DB_PATH)) as con:
        con.execute(
            f"INSERT INTO accounts ({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT (id) DO UPDATE SET {updates}",
            values,
        )


def insert_transactions(txns: list[dict]) -> int:
    """Insert rows, skipping any already present. Returns the NEW row count.

    Ids are minted here if absent, and written back onto the caller's dicts so
    later stages can address the rows they just stored.

    ON CONFLICT DO NOTHING rather than DO UPDATE: a row already in the
    database may since have been categorized, or flagged as a transfer or as
    recurring by stages that run after this one. Overwriting it with the freshly
    adapted version would silently discard all of that.
    """
    if not txns:
        return 0

    assign_txn_ids(txns)

    # Within-batch duplicates are impossible once occurrence numbers are
    # assigned, but a caller can hand us pre-set ids. Keep the first of any
    # repeat so the batch cannot violate its own primary key.
    batch, batch_ids = [], set()
    for txn in txns:
        if txn["id"] in batch_ids:
            logger.warning("Dropping duplicate id %s within the batch", txn["id"])
            continue
        batch_ids.add(txn["id"])
        batch.append(tuple(txn.get(c) for c in TRANSACTION_COLUMNS))

    placeholders = ", ".join("?" for _ in TRANSACTION_COLUMNS)
    with duckdb.connect(str(DB_PATH)) as con:
        before = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        con.executemany(
            f"INSERT INTO transactions ({', '.join(TRANSACTION_COLUMNS)}) "
            f"VALUES ({placeholders}) ON CONFLICT (id) DO NOTHING",
            batch,
        )
        after = con.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]

    inserted = after - before
    logger.info("Inserted %d new of %d submitted (%d already present)",
                inserted, len(txns), len(txns) - inserted)
    return inserted


def all_transactions() -> list[dict]:
    """Every stored row, oldest first, as canonical dicts.

    Transfers pair across accounts and across uploads, and recurring
    detection needs a merchant's whole history, so both have to see
    everything already stored -- not just the statement being uploaded.
    """
    with get_con(read_only=True) as con:
        rows = con.execute(
            f"SELECT {', '.join(TRANSACTION_COLUMNS)} FROM transactions "
            "ORDER BY date, id"
        ).fetchall()
    return [dict(zip(TRANSACTION_COLUMNS, row)) for row in rows]


def update_flags(txns: list[dict]) -> int:
    """Write is_transfer / is_recurring back. Returns rows updated.

    Only these two columns are touched. A blanket UPDATE would overwrite
    category and merchant, which later stages may have enriched since the
    row was inserted.
    """
    updates = [(bool(t.get("is_transfer")), bool(t.get("is_recurring")), t["id"])
               for t in (txns or []) if isinstance(t, dict) and t.get("id")]
    if not updates:
        return 0
    with duckdb.connect(str(DB_PATH)) as con:
        con.executemany(
            "UPDATE transactions SET is_transfer = ?, is_recurring = ? WHERE id = ?",
            updates,
        )
    logger.info("Updated flags on %d row(s)", len(updates))
    return len(updates)


def record_extraction(account_id: str, result: dict) -> None:
    """Store one statement's reconciliation verdict.

    Keyed on (account, uploaded_at) rather than account alone: a second
    statement for the same card is a separate extraction with its own
    verdict, and overwriting would hide a failure behind a later success.
    """
    stamp = datetime.now(timezone.utc)
    row_id = hashlib.sha256(
        f"{account_id}|{stamp.isoformat()}".encode()).hexdigest()[:16]
    flagged = result.get("rows_needing_review") or []
    with duckdb.connect(str(DB_PATH)) as con:
        con.execute(
            "INSERT INTO extractions VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (id) DO NOTHING",
            [row_id, account_id, stamp,
             bool(result.get("reconciled")), float(result.get("delta") or 0.0),
             int(result.get("checks_run") or 0), len(flagged)],
        )


def extraction_summary() -> dict:
    """Every statement's verdict, collapsed for the dashboard badge.

    reconciled is True only if every statement reconciled AND at least one
    check actually ran. An upload with nothing checkable must not display a
    green badge -- that is the same false-confidence rule validate.py
    applies per statement, applied again across statements.
    """
    with get_con(read_only=True) as con:
        row = con.execute(
            "SELECT COUNT(*), COUNT(*) FILTER (WHERE reconciled), "
            "       COALESCE(SUM(delta), 0), COALESCE(SUM(rows_flagged), 0), "
            "       COALESCE(SUM(checks_run), 0) "
            "FROM extractions"
        ).fetchone()

    total, passed, delta, flagged, checks = row
    return {
        "reconciled": bool(total) and passed == total and checks > 0,
        "delta": round(float(delta), 2),
        "rows_needing_review": int(flagged),
    }
