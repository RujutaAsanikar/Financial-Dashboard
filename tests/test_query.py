"""Stage 10 tests. Offline -- no test reaches the model.

The SQL guard is the point: this is the one place a model's output is
executed, so the validator gets the attention.
"""
import pytest

import db
import main
import query
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    # The live-call guard lives in conftest.py. It used to be attempted here as
    # setattr(query._client, "__call__", ...), which never fired -- Python looks
    # up __call__ on the type, so query._client() still reached the real one.
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.duckdb")
    db.reset_db()
    yield


# --- CLAUDE.md's five required cases --------------------------------------

def test_a_plain_select_passes():
    assert query.validate_sql("SELECT 1")


@pytest.mark.parametrize("sql", [
    "DROP TABLE transactions",
    "INSERT INTO transactions VALUES (1)",
    "UPDATE transactions SET amount = 0",
    "DELETE FROM transactions",
    "CREATE TABLE evil (x INT)",
    "ATTACH 'evil.db'",
])
def test_non_select_statements_are_rejected(sql):
    with pytest.raises(ValueError):
        query.validate_sql(sql)


def test_multi_statement_is_rejected():
    with pytest.raises(ValueError, match="one statement"):
        query.validate_sql("SELECT 1; DROP TABLE transactions")


def test_a_comment_mentioning_drop_is_accepted():
    """Proves we parse rather than string-match."""
    assert query.validate_sql("SELECT 1 -- DROP TABLE transactions")
    assert query.validate_sql("/* DELETE FROM accounts */ SELECT 1")


# --- the guard's edges -----------------------------------------------------

@pytest.mark.parametrize("sql", ["", "   ", None, "not sql at all ((("])
def test_junk_is_rejected(sql):
    with pytest.raises(ValueError):
        query.validate_sql(sql)


def test_markdown_fences_are_stripped():
    assert "SELECT" in query.validate_sql("```sql\nSELECT 1\n```")
    assert "```" not in query.validate_sql("```sql\nSELECT 1\n```")


def test_a_trailing_semicolon_is_tolerated():
    assert query.validate_sql("SELECT 1;")


def test_a_read_only_connection_cannot_write():
    with db.get_con(read_only=True) as con:
        with pytest.raises(Exception):
            con.execute("DELETE FROM transactions")


def test_results_are_capped():
    with db.get_con() as con:
        con.executemany(
            "INSERT INTO transactions (id, amount) VALUES (?, ?)",
            [(str(i), -1.0) for i in range(query.ROW_LIMIT + 50)])
    assert len(query.run_safe_query("SELECT * FROM transactions")) == query.ROW_LIMIT


# --- the prompt ------------------------------------------------------------

def test_the_prompt_carries_what_the_model_needs():
    prompt = query.build_sql_prompt("q", query.SCHEMA_DDL, query.CATEGORIES, "2026-09-20")
    assert "transactions(" in prompt
    assert "Food & Drink" in prompt and "Fees & Interest" in prompt
    assert "2026-09-20" in prompt
    assert "NEGATIVE" in prompt          # the sign convention
    assert "is_transfer" in prompt       # the exclusion rule
    assert "refusal" in prompt           # restricted questions


def test_the_prompt_states_the_refusal_contract():
    """The anti-hallucination clauses are the point of this prompt, not decoration."""
    prompt = query.build_sql_prompt("q", query.SCHEMA_DDL, query.CATEGORIES, "2026-09-20")
    assert "That isn't answerable from the statement data available." in prompt
    assert "WHEN TO REFUSE" in prompt
    for forbidden in ("advice", "future", "credit score", "general knowledge"):
        assert forbidden in prompt.lower(), forbidden
    assert "never approximate" in prompt.lower()


def test_four_suggested_questions():
    assert len(query.SUGGESTED_QUESTIONS) == 4


# --- an empty result is never narrated into a figure -----------------------

def test_no_rows_is_an_empty_result():
    assert query.is_empty_result([]) is True


def test_an_all_null_aggregate_row_is_an_empty_result():
    """SUM() over nothing returns one NULL row, not zero rows.

    This is the whole reason the helper exists: the shape says "I have an
    answer" while the content says "there was nothing to answer from".
    """
    assert query.is_empty_result([{"total_spent": None}]) is True
    assert query.is_empty_result([{"month": None, "total": None}]) is True


def test_a_real_value_is_not_an_empty_result():
    assert query.is_empty_result([{"total_spent": 0.0}]) is False
    assert query.is_empty_result([{"merchant": "Netflix", "total": None}]) is False


def test_an_empty_result_short_circuits_the_summarizer(monkeypatch):
    """The model is never handed an empty result to narrate."""
    sql = "SELECT SUM(amount) AS total FROM transactions WHERE category = 'Nope'"
    monkeypatch.setattr(query, "_generate_sql", lambda q, error=None: (sql, None))
    monkeypatch.setattr(query, "run_safe_query", lambda s: [{"total": None}])

    def must_not_run(*a, **k):
        raise AssertionError("_summarize was called on an empty result")
    monkeypatch.setattr(query, "_summarize", must_not_run)

    result = query.answer_question("How much did I spend on my yacht?")
    assert result["no_data"] is True
    assert result["answer"] == query.NO_DATA
    assert result["sql"] == sql          # the SQL is still shown; only the prose changes


def test_a_populated_result_still_reaches_the_summarizer(monkeypatch):
    monkeypatch.setattr(query, "_generate_sql",
                        lambda q, error=None: ("SELECT 1 AS n", None))
    monkeypatch.setattr(query, "run_safe_query", lambda s: [{"n": 1}])
    seen = {}

    def summarize(q, rows, sql=""):
        seen["sql"] = sql
        return "It is 1."
    monkeypatch.setattr(query, "_summarize", summarize)

    result = query.answer_question("what is 1")
    assert result["answer"] == "It is 1."
    assert "no_data" not in result
    # The SQL must reach the summarizer -- without it the model cannot tell
    # that the rows answer the question, and refuses valid results.
    assert seen["sql"] == "SELECT 1 AS n"


# --- answer_question never raises -----------------------------------------

def test_a_refusal_is_returned_without_running_sql(monkeypatch):
    monkeypatch.setattr(query, "_generate_sql",
                        lambda q, error=None: ("", "I can't give investment advice."))
    result = query.answer_question("Should I buy Tesla stock?")
    assert result["refused"] is True
    assert result["sql"] == ""
    assert result["rows"] == []


def test_a_dead_model_degrades_to_a_message(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no key")
    monkeypatch.setattr(query, "_generate_sql", boom)
    result = query.answer_question("anything")
    assert result["rows"] == []
    assert "could not" in result["answer"].lower()


def test_bad_sql_is_retried_once_then_gives_up(monkeypatch):
    calls = []
    def gen(q, error=None):
        calls.append(error)
        return "SELECT nonexistent_column FROM transactions", None
    monkeypatch.setattr(query, "_generate_sql", gen)

    result = query.answer_question("q")
    assert len(calls) == 2, "should retry exactly once"
    assert calls[1] is not None, "the error must be fed back"
    assert result["rows"] == []


def test_an_empty_question_is_handled():
    assert query.answer_question("")["rows"] == []


# --- the endpoint ----------------------------------------------------------

def test_ask_endpoint_rejects_an_empty_question():
    client = TestClient(main.app)
    assert client.post("/api/ask", json={"question": "  "}).status_code == 400
    assert client.post("/api/ask", json={}).status_code == 400


def test_ask_endpoint_rejects_an_overlong_question():
    client = TestClient(main.app)
    assert client.post("/api/ask", json={"question": "x" * 600}).status_code == 400


def test_suggestions_endpoint():
    response = TestClient(main.app).get("/api/ask/suggestions")
    assert response.status_code == 200
    assert len(response.json()["questions"]) == 4


def test_ask_returns_the_shape_the_frontend_renders(monkeypatch):
    monkeypatch.setattr(query, "answer_question",
                        lambda q: {"answer": "Yes.", "sql": "SELECT 1", "rows": [{"a": 1}]})
    body = TestClient(main.app).post("/api/ask", json={"question": "hi"}).json()
    assert set(body) >= {"answer", "sql", "rows"}
