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
import re
import threading

import sqlglot
import sqlglot.expressions as exp

import db

logger = logging.getLogger(__name__)

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
  apr DOUBLE, credit_limit DOUBLE
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
    "Compare August to July spending.",
]


def build_sql_prompt(question: str, schema_ddl: str, categories: str, today) -> str:
    """The system prompt. Kept a pure function so it can be tested offline."""
    return f"""\
You translate a question about personal bank transactions into ONE DuckDB
SELECT statement. You never see the data and you never compute a figure --
the database does that.

Schema:
{schema_ddl}

The only values `category` ever takes:
{categories}

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

If the question cannot be answered from this schema -- it asks for advice,
for data the schema does not hold (merchant addresses, interest projections,
anything about the future), or is not about these transactions at all -- set
`refusal` to one short sentence saying what you cannot answer and leave `sql`
empty. Do not invent a query that approximates the question.

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
            return [dict(zip(columns, row)) for row in cursor.fetchall()]
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

    prompt = build_sql_prompt(question, SCHEMA_DDL, CATEGORIES, date.today().isoformat())
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


def _summarize(question: str, rows: list[dict]) -> str:
    """One sentence, from the RESULT ROWS ONLY. The model never sees the table."""
    import json

    response = _client().messages.create(
        model=MODEL, max_tokens=300,
        output_config={"effort": "low"},
        system="Answer the question in ONE short sentence using only the rows "
               "given. Quote the figures exactly as they appear; do not "
               "recompute, round or add commentary. If the rows are empty, say "
               "no matching transactions were found.",
        messages=[{"role": "user",
                   "content": f"Question: {question}\nRows: {json.dumps(rows[:50], default=str)}"}],
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

    try:
        answer = _summarize(question, rows)
    except Exception as exc:
        logger.warning("Summarize failed: %s", exc)
        answer = f"Found {len(rows)} row(s)." if rows else "No matching transactions."

    return {"answer": answer or f"Found {len(rows)} row(s).", "sql": sql, "rows": rows}
