"""FastAPI app and routes.

/api/dashboard is backed by DuckDB as of Stage 9. The Stage 0 mock is kept
and served from /api/mock-dashboard: the frontend was built against it, and
deleting it would break their dev loop the moment the database is empty --
which it is after every demo reset.

Every endpoint returns a JSON error rather than a traceback. An upload is the
one place a user can hand us arbitrary input, so its failure modes are
enumerated explicitly.
"""

import json
import logging

from fastapi import Body, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware

import aggregate
import db
import query
from adapter import normalize_account_type
from models import DashboardResponse, HealthResponse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Finance Dashboard API", version="0.1.0")

# Any localhost port, so the frontend's dev server port doesn't matter.
# Add an explicit origin here if the frontend runs on a LAN IP or a tunnel.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _series(balances: list[float]) -> list[dict]:
    return [{"month": i, "balance": b} for i, b in enumerate(balances, start=1)]


# Hardcoded stand-in until Stage 9 wires up the real aggregation.
MOCK_DASHBOARD: dict = {
    "accounts": [
        {
            "id": "chase-4821",
            "bank_name": "Chase",
            "account_holder_name": "Jane Doe",
            "account_last4": "4821",
            "account_type": "credit",
            "closing_balance": 3204.18,
            "apr": 24.99,
            "transaction_count": 87,
        },
        {
            "id": "pnc-1093",
            "bank_name": "PNC",
            "account_holder_name": "Jane Doe",
            "account_last4": "1093",
            "account_type": "checking",
            "closing_balance": 5417.62,
            "apr": None,
            "transaction_count": 134,
        },
    ],
    "summary": {
        "total_spent": 12847.33,
        "total_income": 14202.50,
        "net": 1355.17,
        "transaction_count": 221,
        "period_start": "2026-03-01",
        "period_end": "2026-09-17",
    },
    "by_category": [
        {"category": "Housing", "amount": 4200.00, "count": 6},
        {"category": "Groceries", "amount": 2184.55, "count": 41},
        {"category": "Food & Drink", "amount": 1893.22, "count": 58},
        {"category": "Subscriptions", "amount": 1129.88, "count": 33},
        {"category": "Transportation", "amount": 942.17, "count": 24},
        {"category": "Shopping", "amount": 878.40, "count": 17},
        {"category": "Utilities", "amount": 731.05, "count": 12},
        {"category": "Health", "amount": 402.16, "count": 7},
        {"category": "Entertainment", "amount": 289.50, "count": 9},
        {"category": "Travel", "amount": 118.90, "count": 2},
        {"category": "Fees & Interest", "amount": 77.50, "count": 8},
    ],
    "spending_over_time": [
        {"month": "2026-03", "amount": 1982.44},
        {"month": "2026-04", "amount": 1755.20},
        {"month": "2026-05", "amount": 2034.87},
        {"month": "2026-06", "amount": 1891.33},
        {"month": "2026-07", "amount": 1702.65},
        {"month": "2026-08", "amount": 2218.49},
        {"month": "2026-09", "amount": 1262.35},
    ],
    "subscriptions": [
        {
            "merchant": "Duquesne Light",
            "amount": 96.40,
            "cadence_days": 30,
            "annual_cost": 1172.87,
            "price_change_pct": None,
            "first_seen": "2026-03-12",
            "next_expected": "2026-10-12",
            "account_id": "pnc-1093",
        },
        {
            "merchant": "Verizon Wireless",
            "amount": 85.00,
            "cadence_days": 30,
            "annual_cost": 1034.17,
            "price_change_pct": None,
            "first_seen": "2026-03-08",
            "next_expected": "2026-10-08",
            "account_id": "pnc-1093",
        },
        {
            "merchant": "State Farm Insurance",
            "amount": 142.50,
            "cadence_days": 90,
            "annual_cost": 577.92,
            "price_change_pct": None,
            "first_seen": "2026-03-20",
            "next_expected": "2026-12-16",
            "account_id": "pnc-1093",
        },
        {
            "merchant": "Planet Fitness",
            "amount": 24.99,
            "cadence_days": 30,
            "annual_cost": 304.05,
            "price_change_pct": 8.7,
            "first_seen": "2026-03-05",
            "next_expected": "2026-10-05",
            "account_id": "chase-4821",
        },
        {
            "merchant": "Adobe Creative Cloud",
            "amount": 22.99,
            "cadence_days": 30,
            "annual_cost": 279.71,
            "price_change_pct": 9.5,
            "first_seen": "2026-03-18",
            "next_expected": "2026-10-18",
            "account_id": "chase-4821",
        },
        {
            "merchant": "Nytimes",
            "amount": 4.25,
            "cadence_days": 7,
            "annual_cost": 221.61,
            "price_change_pct": None,
            "first_seen": "2026-03-02",
            "next_expected": "2026-09-21",
            "account_id": "chase-4821",
        },
        {
            "merchant": "Netflix.Com",
            "amount": 17.99,
            "cadence_days": 30,
            "annual_cost": 218.88,
            "price_change_pct": 12.5,
            "first_seen": "2026-03-15",
            "next_expected": "2026-10-15",
            "account_id": "chase-4821",
        },
        {
            "merchant": "Spotifyusa",
            "amount": 11.99,
            "cadence_days": 30,
            "annual_cost": 145.88,
            "price_change_pct": None,
            "first_seen": "2026-03-11",
            "next_expected": "2026-10-11",
            "account_id": "chase-4821",
        },
        {
            "merchant": "Amazon Prime",
            "amount": 139.00,
            "cadence_days": 365,
            "annual_cost": 139.00,
            "price_change_pct": None,
            "first_seen": "2026-04-22",
            "next_expected": "2027-04-22",
            "account_id": "chase-4821",
        },
        {
            "merchant": "Peacock",
            "amount": 7.99,
            "cadence_days": 30,
            "annual_cost": 97.21,
            "price_change_pct": None,
            "first_seen": "2026-03-24",
            "next_expected": "2026-10-24",
            "account_id": "chase-4821",
        },
        {
            "merchant": "Apple Icloud",
            "amount": 2.99,
            "cadence_days": 30,
            "annual_cost": 36.38,
            "price_change_pct": None,
            "first_seen": "2026-03-09",
            "next_expected": "2026-10-09",
            "account_id": "chase-4821",
        },
    ],
    "subscription_totals": {
        "count": 11,
        "annual_cost": 4227.68,
        "price_increases": 3,
    },
    "repeated_spending": [
        {
            "merchant": "Giant Eagle", "amount": 86.40, "cadence_days": 7,
            "annual_cost": 4505.83, "occurrences": 26,
            "first_seen": "2026-03-07", "last_seen": "2026-09-12",
            "account_id": "pnc-5590",
        },
        {
            "merchant": "Coffee Tree Roasters", "amount": 5.25, "cadence_days": 7,
            "annual_cost": 273.75, "occurrences": 27,
            "first_seen": "2026-03-03", "last_seen": "2026-09-15",
            "account_id": "chase-4821",
        },
        {
            "merchant": "Shell", "amount": 48.20, "cadence_days": 14,
            "annual_cost": 1256.79, "occurrences": 13,
            "first_seen": "2026-03-11", "last_seen": "2026-09-09",
            "account_id": "chase-4821",
        },
        {
            "merchant": "Uber Eats", "amount": 31.75, "cadence_days": 14,
            "annual_cost": 827.77, "occurrences": 12,
            "first_seen": "2026-03-18", "last_seen": "2026-09-02",
            "account_id": "chase-4821",
        },
    ],
    "repeated_spending_totals": {"count": 4, "annual_cost": 6864.14},
    "payoff": [
        {
            "account_id": "chase-4821",
            "balance": 3204.18,
            "apr": 24.99,
            "scenarios": [
                {
                    "label": "Minimum only",
                    "monthly_payment": 98.77,
                    "months": 55,
                    "total_interest": 2190.54,
                    "series": _series(
                        [
                            3172.14, 3139.43, 3106.04, 3071.95, 3037.15,
                            3001.63, 2965.37, 2928.35, 2890.56, 2851.99,
                            2812.61, 2772.41, 2731.38, 2689.49, 2646.73,
                            2603.08, 2558.52, 2513.03, 2466.59, 2419.19,
                            2370.80, 2321.40, 2270.97, 2219.49, 2166.94,
                            2113.30, 2058.54, 2002.64, 1945.57, 1887.32,
                            1827.85, 1767.14, 1705.17, 1641.91, 1577.33,
                            1511.41, 1444.12, 1375.42, 1305.29, 1233.70,
                            1160.62, 1086.02, 1009.87, 932.13, 852.77,
                            771.76, 689.06, 604.64, 518.46, 430.49,
                            340.68, 249.00, 155.42, 59.89, 0.00,
                        ]
                    ),
                },
                {
                    "label": "Pay off in 3 years",
                    "monthly_payment": 127.39,
                    "months": 36,
                    "total_interest": 1381.34,
                    "series": _series(
                        [
                            3143.52, 3081.59, 3018.37, 2953.84, 2887.96,
                            2820.71, 2752.06, 2681.98, 2610.44, 2537.41,
                            2462.86, 2386.76, 2309.07, 2229.77, 2148.81,
                            2066.17, 1981.81, 1895.69, 1807.78, 1718.04,
                            1626.43, 1532.91, 1437.44, 1339.98, 1240.50,
                            1138.94, 1035.27, 929.44, 821.41, 711.13,
                            598.55, 483.62, 366.30, 246.54, 124.28,
                            0.00,
                        ]
                    ),
                },
                {
                    "label": "Minimum + $100",
                    "monthly_payment": 198.77,
                    "months": 20,
                    "total_interest": 740.60,
                    "series": _series(
                        [
                            3072.14, 2937.35, 2799.75, 2659.28, 2515.89,
                            2369.51, 2220.09, 2067.55, 1911.84, 1752.88,
                            1590.61, 1424.96, 1255.86, 1083.24, 907.03,
                            727.15, 543.52, 356.07, 164.72, 0.00,
                        ]
                    ),
                },
                {
                    "label": "Pay off in 1 year",
                    "monthly_payment": 304.53,
                    "months": 12,
                    "total_interest": 450.09,
                    "series": _series(
                        [
                            2966.38, 2723.62, 2475.81, 2222.84, 1964.60,
                            1700.98, 1431.87, 1157.16, 876.73, 590.46,
                            298.23, 0.00,
                        ]
                    ),
                },
            ],
        }
    ],
    "transfers_excluded": {"count": 4, "total": 1850.00},
    "extraction": {"reconciled": True, "delta": 0.0, "rows_needing_review": 0},
}


@app.get("/api/health", response_model=HealthResponse)
def health() -> dict:
    return {"ok": True}


MAX_UPLOAD_BYTES = 10 * 1024 * 1024


@app.get("/api/dashboard", response_model=DashboardResponse)
def get_dashboard() -> dict:
    """The whole dashboard, from the database."""
    try:
        return aggregate.build_dashboard()
    except Exception as exc:
        logger.exception("Dashboard build failed")
        raise HTTPException(status_code=500,
                            detail=f"Could not build the dashboard: {exc}") from exc


@app.get("/api/mock-dashboard", response_model=DashboardResponse)
def get_mock_dashboard() -> dict:
    """The Stage 0 fixture, unchanged.

    Kept so the frontend has something to render against an empty database.
    """
    return MOCK_DASHBOARD


@app.post("/api/upload", response_model=DashboardResponse)
async def upload(
    file: UploadFile = File(...),
    apr: float | None = Form(default=None),
    credit_limit: float | None = Form(default=None),
    account_nickname: str | None = Form(default=None),
) -> dict:
    """Ingest one parser JSON file and return the updated dashboard."""
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File is {len(raw) // 1024}KB; the limit is "
                   f"{MAX_UPLOAD_BYTES // 1024 // 1024}MB.")

    try:
        parser_json = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400,
                            detail=f"That file is not valid JSON: {exc}") from exc

    try:
        result = aggregate.ingest(parser_json, apr=apr, credit_limit=credit_limit,
                                  account_nickname=account_nickname)
    except ValueError as exc:
        # adapt() raises this when the payload is not parser output at all.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("Upload failed")
        raise HTTPException(status_code=500,
                            detail=f"Could not process that statement: {exc}") from exc

    logger.info("Upload %s: %d new of %d rows", file.filename,
                result["inserted"], result["submitted"])
    return aggregate.build_dashboard()


@app.post("/api/upload-batch", response_model=DashboardResponse)
async def upload_batch(
    files: list[UploadFile] = File(...),
    apr: float | None = Form(default=None),
    credit_limit: float | None = Form(default=None),
) -> dict:
    """Ingest several statements in one request.

    Preferred over looping /api/upload from the client. Transfer detection
    runs across accounts, so sending a chequing and a credit statement
    separately leaves the card payment counted as spending until the second
    one lands -- the dashboard visibly corrects itself mid-demo.

    apr and credit_limit apply to whichever statement turns out to be a
    credit account; they are ignored on the others, so one form serves a
    mixed batch.

    A file that fails is skipped rather than failing the batch: five good
    statements should not be lost because the sixth was a holiday snap. The
    whole batch failing is the only 422.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files were uploaded.")

    ingested, failures = [], []
    for upload_file in files:
        raw = await upload_file.read()
        name = upload_file.filename or "unnamed"

        if not raw:
            failures.append({"file": name, "error": "The file is empty."})
            continue
        if len(raw) > MAX_UPLOAD_BYTES:
            failures.append({"file": name, "error": "File is too large."})
            continue

        try:
            parser_json = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            failures.append({"file": name, "error": f"Not valid JSON: {exc}"})
            continue

        # One form serves a mixed batch, so apr only reaches the statement it
        # describes. Applying it to every file would stamp an APR onto a
        # chequing account and surface it in accounts[].apr.
        is_credit = (
            isinstance(parser_json, dict)
            and normalize_account_type(parser_json.get("account_type")) == "credit"
        )

        try:
            result = aggregate.ingest(
                parser_json,
                apr=apr if is_credit else None,
                credit_limit=credit_limit if is_credit else None,
            )
            ingested.append({"file": name, **result})
        except ValueError as exc:
            failures.append({"file": name, "error": str(exc)})
        except Exception as exc:
            logger.exception("Batch upload failed on %s", name)
            failures.append({"file": name, "error": str(exc)})

    if failures:
        logger.warning("Batch: %d of %d file(s) failed: %s",
                       len(failures), len(files),
                       "; ".join(f["file"] for f in failures))
    if not ingested:
        raise HTTPException(
            status_code=422,
            detail="No file could be processed. "
                   + "; ".join(f'{f["file"]}: {f["error"]}' for f in failures))

    logger.info("Batch: ingested %d of %d file(s), %d new row(s)",
                len(ingested), len(files), sum(i["inserted"] for i in ingested))
    return aggregate.build_dashboard()


@app.get("/api/transactions")
def get_transactions(
    limit: int = Query(default=100, ge=1, le=1000),
    category: str | None = None,
    account_id: str | None = None,
) -> dict:
    """Recent transactions, newest first, with optional filters."""
    clauses, params = [], []
    if category:
        clauses.append("category = ?")
        params.append(category)
    if account_id:
        clauses.append("account_id = ?")
        params.append(account_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    try:
        db.init_db()
        with db.get_con(read_only=True) as con:
            total = con.execute(
                f"SELECT COUNT(*) FROM transactions {where}", params).fetchone()[0]
            rows = con.execute(
                f"SELECT {', '.join(db.TRANSACTION_COLUMNS)} FROM transactions "
                f"{where} ORDER BY date DESC, id LIMIT ?", [*params, limit]).fetchall()
    except Exception as exc:
        logger.exception("Transaction query failed")
        raise HTTPException(status_code=500,
                            detail=f"Could not read transactions: {exc}") from exc

    transactions = [dict(zip(db.TRANSACTION_COLUMNS, row)) for row in rows]
    for txn in transactions:
        txn["date"] = txn["date"].isoformat() if txn["date"] else None
    return {"transactions": transactions, "count": len(transactions), "total": total}


@app.post("/api/reset")
def reset() -> dict:
    """Wipe every table. For demo resets."""
    try:
        db.reset_db()
    except Exception as exc:
        logger.exception("Reset failed")
        raise HTTPException(status_code=500,
                            detail=f"Could not reset the database: {exc}") from exc
    return {"ok": True, "message": "Database reset."}


@app.get("/api/ask/suggestions")
def ask_suggestions() -> dict:
    """The questions the demo clicks. Never typed live on stage."""
    return {"questions": query.SUGGESTED_QUESTIONS}


@app.post("/api/ask")
def ask(payload: dict = Body(...)) -> dict:
    """Answer a question about the stored transactions.

    The model writes SQL; DuckDB computes. Guarded three ways -- sqlglot
    parses and rejects anything that is not a single SELECT, the query is
    wrapped in a LIMIT, and the connection is read-only.
    """
    question = (payload or {}).get("question", "")
    if not isinstance(question, str) or not question.strip():
        raise HTTPException(status_code=400, detail="Ask a question.")
    if len(question) > 500:
        raise HTTPException(status_code=400, detail="That question is too long.")

    try:
        db.init_db()
        result = query.answer_question(question)
    except Exception as exc:
        logger.exception("Ask failed")
        raise HTTPException(status_code=500, detail=f"Could not answer that: {exc}") from exc

    result.setdefault("rows", [])
    result.setdefault("sql", "")
    return result
