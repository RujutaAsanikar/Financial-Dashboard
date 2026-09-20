"""Natural-language questions -> SQL -> answer. Stage 10.

The model writes SQL; DuckDB does the arithmetic. The model never sees the
transaction data and never computes a figure, so every number on screen came
out of the database.

Three independent guards, because this is the one place a model's output is
executed:

  1. sqlglot PARSES the statement. Exactly one node, and it must be a Select.
     Parsing rather than string-matching means a comment containing the word
     DROP is fine and a genuine second statement is not.
  2. The query is wrapped in SELECT * FROM (...) LIMIT 500, so an unbounded
     scan cannot be returned.
  3. The connection is READ-ONLY. Even if 1 and 2 were both defeated, DuckDB
     itself refuses to write.

Questions the schema cannot answer are refused up front rather than answered
with invented SQL.
"""

import logging
import os
import re
import threading
from pathlib import Path

import sqlglot
import sqlglot.expressions as exp

import db

logger = logging.getLogger(__name__)


def _load_env() -> None:
    """Read .env into the environment, if it exists.

    The Anthropic client reads ANTHROPIC_API_KEY from the environment, so
    `uvicorn main:app` started from a plain shell has no key and every
    /api/ask degrades to "could not reach the model" -- a silent failure that
    looks like a bug in this module. It lives here rather than in main.py so
    the Q&A feature carries its own configuration.

    Hand-rolled rather than adding python-dotenv, which is not in CLAUDE.md's
    dependency list. Real environment variables always win, so CI and
    deployment are unaffected.
    """
    env_file = Path(__file__).with_name(".env")
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        os.environ.setdefault(name.strip(), value.strip().strip("'\""))


_load_env()

MODEL = "claude-opus-4-8"
ROW_LIMIT = 500
TIMEOUT_SECONDS = 20        # model calls
QUERY_TIMEOUT_SECONDS = 3   # SQL execution, per CLAUDE.md

SCHEMA_DDL = """
transactions(
  id VARCHAR, account_id VARCHAR, date DATE, description VARCHAR,
  merchant VARCHAR,        -- normalized name, group by this not description
  category VARCHAR, confidence DOUBLE,
  amount DOUBLE,           -- SIGNED: negative = money out, positive = money in
  balance DOUBLE, reference VARCHAR,
  is_transfer BOOLEAN,     -- money moved between the user's own accounts
  is_recurring BOOLEAN
)
accounts(
  id VARCHAR, bank_name VARCHAR, account_holder_name VARCHAR,
  account_last4 VARCHAR, account_type VARCHAR,  -- checking|savings|credit|unknown
  currency VARCHAR, opening_balance DOUBLE, closing_balance DOUBLE,
  apr DOUBLE
)
"""

CATEGORIES = (
    "Food & Drink, Groceries, Transportation, Shopping, Entertainment, "
    "Subscriptions, Utilities, Housing, Health, Education, Travel, "
    "Income, Transfer, Fees & Interest, Other"
)

SUGGESTED_QUESTIONS = [
    "How much did I spend on food last month?",
    "What is my biggest recurring charge?",
    "How much do I spend on weekends?",
    "Compare my spending week by week.",
]


MERCHANT_LIST_CAP = 150


def data_inventory() -> str:
    """The actual values of the columns a question is most likely to filter on.

    The prompt has always listed the exact `category` values, so category
    questions work. It said nothing about `bank_name` or `merchant`, so the
    model had to guess them -- and guessed wrong: asked to compare two banks
    it wrote IN ('Finance Bank', 'Wiki Bank') against stored values of
    'FINANCE BANK' and 'FIRST BANK OF WIKI'. Equality is exact, so the query
    returned nothing and the user got "no transactions" for a question the
    data could answer.

    Accounts are always few. Merchants are capped -- past the cap the list
    stops being worth the tokens, and the ILIKE rule in the prompt covers it.
    Never raises: a missing or empty database just yields no inventory.
    """
    try:
        with db.get_con(read_only=True) as con:
            accounts = con.execute(
                "SELECT bank_name, account_type, account_last4 FROM accounts "
                "ORDER BY bank_name"
            ).fetchall()
            merchants = con.execute(
                "SELECT DISTINCT merchant FROM transactions "
                "WHERE merchant IS NOT NULL ORDER BY merchant"
            ).fetchall()
    except Exception as exc:                      # pragma: no cover - defensive
        logger.warning("Could not read the data inventory: %s", exc)
        return ""

    lines = []
    if accounts:
        lines.append("The ONLY accounts that exist (bank_name is stored exactly "
                     "as written here):")
        for bank, kind, last4 in accounts:
            lines.append(f"  - {bank!r}  type={kind}  last4={last4}")
    if merchants and len(merchants) <= MERCHANT_LIST_CAP:
        names = ", ".join(repr(m[0]) for m in merchants)
        lines.append(f"\nThe ONLY merchants that exist: {names}")
    return "\n".join(lines)


def build_sql_prompt(question: str, schema_ddl: str, categories: str, today,
                     inventory: str = "") -> str:
    """The system prompt. Kept a pure function so it can be tested offline."""
    inventory_block = f"\n{inventory}\n" if inventory else ""
    return f"""\
You translate a question about personal bank transactions into ONE DuckDB
SELECT statement. You never see the data and you never compute a figure --
the database does that.

Schema:
{schema_ddl}

The only values `category` ever takes:
{categories}
{inventory_block}
Today is {today}.

Rules:
- `amount` is SIGNED. Spending is NEGATIVE. For "how much did I spend", use
  ABS(SUM(amount)) with a `WHERE amount < 0` filter, and return a positive
  number.
- Exclude transfers from any spending or income figure:
  `AND NOT COALESCE(is_transfer, FALSE)`. They are money moving between the
  user's own accounts, not spending.
- Group merchants by the `merchant` column, never `description` -- the raw
  description contains store numbers and varies between visits.
- Return ONE SELECT statement. No semicolon, no markdown fence, no commentary,
  no CTE chains that end in anything but a SELECT.
- Never write INSERT, UPDATE, DELETE, DROP, CREATE, ALTER or ATTACH.
- Use ONLY the tables and columns listed above. If answering would need a
  column that is not in the schema, that is a refusal, not a reason to
  substitute a different column.
- NEVER invent a value for `bank_name`, `merchant` or `account_type`. Copy it
  character-for-character from the inventory above. `=` and `IN` are exact,
  so a guessed spelling silently returns zero rows and the user is told there
  are no such transactions -- a wrong answer that looks like a fact.
- When filtering on `bank_name` or `merchant`, use ILIKE with wildcards
  (`merchant ILIKE '%netflix%'`) rather than `=`, so capitalisation and
  trailing words cannot cause a false empty result.
- If the question names an account or merchant that is NOT in the inventory,
  refuse and say it is not in the data. Do not fall back to a similar one.

WHEN TO REFUSE
The ONLY thing you may produce is a query that reads the two tables above. A
question is answerable only if every figure it asks for can be computed by
SQL from those columns. If it cannot, set `refusal` and leave `sql` empty.

Refuse, specifically, when the question:
- asks for advice, an opinion, a recommendation, a budget or a plan
  ("should I", "can I afford", "how do I save")
- asks about the future, or for a forecast, projection or estimate
- needs anything the schema does not store: credit scores, account balances
  over time, interest projections, merchant addresses or phone numbers,
  budgets, goals, tax figures, anyone else's data
- is about a period, account, merchant or category that the schema could
  hold but you cannot verify -- write the query anyway and let it return
  nothing; do NOT widen or substitute filters to make rows appear
- is general knowledge, or is not about these bank transactions at all

Your refusal sentence must begin with:
"That isn't answerable from the statement data available."
and may add one short clause naming what is missing.

Never guess, never approximate, never answer from your own knowledge, and
never write a query for a different question than the one asked.

Question: {question}"""


def _strip_fences(sql: str) -> str:
    sql = (sql or "").strip()
    sql = re.sub(r"^```(?:sql)?\s*", "", sql, flags=re.I)
    sql = re.sub(r"\s*```$", "", sql)
    return sql.strip().rstrip(";").strip()


def validate_sql(sql: str) -> str:
    """Return the cleaned SQL, or raise ValueError.

    Parses rather than string-matches: a SELECT whose comment mentions DROP is
    legitimate, and a second statement hidden behind a semicolon is not.
    """
    cleaned = _strip_fences(sql)
    if not cleaned:
        raise ValueError("The model returned no SQL.")

    try:
        statements = sqlglot.parse(cleaned, read="duckdb")
    except Exception as exc:
        raise ValueError(f"Could not parse that SQL: {exc}") from exc

    statements = [s for s in statements if s is not None]
    if len(statements) != 1:
        raise ValueError(f"Expected exactly one statement, got {len(statements)}.")
    if not isinstance(statements[0], exp.Select):
        raise ValueError(f"Only SELECT is allowed, got {type(statements[0]).__name__}.")
    return cleaned


def tidy_floats(rows: list[dict]) -> list[dict]:
    """Round float columns to cents.

    SUM() over doubles accumulates binary residue: summing this schema's
    grocery rows yields 690.3100000000001. The summarizer is told to quote
    figures EXACTLY -- which is what keeps it from inventing them -- so it
    faithfully prints the residue and the user reads it as a real figure.
    Round once, here, and both the answer and the rows table are clean.

    Every float this schema holds is money, an APR, or a 0-1 confidence, so
    2dp is lossless for all of them. Ints, dates and strings are untouched.
    """
    return [
        {k: (round(v, 2) if isinstance(v, float) else v) for k, v in row.items()}
        for row in rows
    ]


def run_safe_query(sql: str) -> list[dict]:
    """Validate, wrap, and execute on a read-only connection."""
    cleaned = validate_sql(sql)
    wrapped = f"SELECT * FROM ({cleaned}) LIMIT {ROW_LIMIT}"

    with db.get_con(read_only=True) as con:
        # DuckDB has no statement_timeout setting; the supported way to bound
        # a query is to run it off-thread and interrupt() from the caller.
        # Without this a model-authored cross join could hang the request.
        timer = threading.Timer(QUERY_TIMEOUT_SECONDS, con.interrupt)
        timer.start()
        try:
            cursor = con.execute(wrapped)
            columns = [d[0] for d in cursor.description]
            return tidy_floats(dict(zip(columns, row)) for row in cursor.fetchall())
        finally:
            timer.cancel()


def _client():
    import anthropic
    return anthropic.Anthropic(timeout=TIMEOUT_SECONDS, max_retries=1)


def _generate_sql(question: str, error: str | None = None) -> tuple[str, str | None]:
    """Ask for SQL. Returns (sql, refusal)."""
    from datetime import date

    from pydantic import BaseModel

    class Generated(BaseModel):
        sql: str
        refusal: str | None = None

    prompt = build_sql_prompt(question, SCHEMA_DDL, CATEGORIES,
                              date.today().isoformat(), data_inventory())
    if error:
        prompt += f"\n\nYour previous query failed with: {error}\nReturn corrected SQL."

    response = _client().messages.parse(
        model=MODEL, max_tokens=2048,
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": prompt}],
        output_format=Generated,
    )
    out = response.parsed_output
    return out.sql or "", (out.refusal or None)


NO_DATA = ("That isn't answerable from the statement data available -- the "
           "query ran but matched no transactions.")

SUMMARY_ROW_LIMIT = 50


def is_empty_result(rows: list[dict]) -> bool:
    """True when the query found nothing, including the aggregate-of-nothing case.

    `SELECT SUM(amount) ... WHERE <matches nothing>` does not return zero rows.
    It returns ONE row whose every value is NULL. Handed that, a summarizer
    reads the shape as a real answer and reports "$0" -- which is a different
    and worse claim than "no such transactions", because $0 sounds verified.
    Treat an all-null result as no result.
    """
    if not rows:
        return True
    return all(value is None for row in rows for value in row.values())


SUMMARY_SYSTEM = """\
You put ONE short sentence around a SQL result. The query below was written to
answer the question and has already run, so the rows ARE the answer -- report
them, do not second-guess whether they are relevant.

Hard rules:
- Every figure in your sentence must appear verbatim in the rows. Copy the
  digits exactly. Do not recompute, re-round, convert, total, average, or
  derive anything the rows do not already state.
- Add nothing from your own knowledge. You know nothing about this person
  beyond these rows.
- If the question asked about several things and the rows cover only some of
  them, say exactly which are present and that the rest returned no data.
  Never fill the gap with a plausible number.
- A dollar amount may be written with a $ and its own digits unchanged.
- No advice, no commentary, no follow-up suggestions."""


def _summarize(question: str, rows: list[dict], sql: str = "") -> str:
    """One sentence, from the RESULT ROWS ONLY. The model never sees the table.

    The SQL goes in as context so the model can see the rows really do answer
    the question. Without it, it cannot tell `{"total_spent": 201.65}` from an
    unrelated number, and a prompt that tells it to refuse on mismatch makes it
    refuse every aggregate -- correct answers included.
    """
    import json

    shown = rows[:SUMMARY_ROW_LIMIT]
    note = ""
    if len(rows) > SUMMARY_ROW_LIMIT:
        # Summarizing 50 of 500 rows as if they were all of them is a wrong
        # total stated confidently. Say so rather than letting it total them.
        note = (f"\nNOTE: only {len(shown)} of {len(rows)} rows are shown. Do "
                "not total or rank them; say the result was too large to "
                "summarize and point at the table below.")

    response = _client().messages.create(
        model=MODEL, max_tokens=300,
        output_config={"effort": "low"},
        system=SUMMARY_SYSTEM,
        messages=[{"role": "user",
                   "content": f"Question: {question}\n"
                              f"Query that produced these rows:\n{sql}\n"
                              f"Rows: {json.dumps(shown, default=str)}{note}"}],
    )
    return next((b.text for b in response.content if b.type == "text"), "").strip()


def answer_question(question: str) -> dict:
    """-> {answer, sql, rows}. Never raises."""
    question = (question or "").strip()
    if not question:
        return {"answer": "Ask me something about your transactions.",
                "sql": "", "rows": []}

    try:
        sql, refusal = _generate_sql(question)
    except Exception as exc:
        logger.warning("SQL generation failed: %s", exc)
        return {"answer": "I could not reach the model to answer that. "
                          "The dashboard figures above are unaffected.",
                "sql": "", "rows": []}

    if refusal:
        return {"answer": refusal, "sql": "", "rows": [], "refused": True}

    # One retry, with the error fed back -- most failures are a wrong column
    # name the model can fix when told.
    for attempt in (1, 2):
        try:
            rows = run_safe_query(sql)
            break
        except Exception as exc:
            logger.warning("Query attempt %d failed: %s", attempt, exc)
            if attempt == 2:
                return {"answer": "I could not turn that into a query I trust. "
                                  "Try rephrasing, or pick one of the suggested "
                                  "questions.",
                        "sql": sql, "rows": []}
            try:
                sql, refusal = _generate_sql(question, error=str(exc))
                if refusal:
                    return {"answer": refusal, "sql": "", "rows": [], "refused": True}
            except Exception:
                return {"answer": "I could not turn that into a query I trust.",
                        "sql": sql, "rows": []}

    # Decided here, not by the model: the model is never given the chance to
    # narrate an empty result into a figure.
    if is_empty_result(rows):
        return {"answer": NO_DATA, "sql": sql, "rows": rows, "no_data": True}

    try:
        answer = _summarize(question, rows, sql)
    except Exception as exc:
        logger.warning("Summarize failed: %s", exc)
        answer = f"Found {len(rows)} row(s)."

    return {"answer": answer or f"Found {len(rows)} row(s).", "sql": sql, "rows": rows}
