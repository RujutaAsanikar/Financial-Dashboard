"""FastAPI app and routes.

Stage 0: health check plus a /api/dashboard endpoint backed by hardcoded
mock data so the frontend can start against the frozen contract.
"""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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
    "payoff": [
        {
            "account_id": "chase-4821",
            "balance": 3204.18,
            "apr": 24.99,
            "scenarios": [
                {
                    "monthly_payment": 150.00,
                    "months": 29,
                    "total_interest": 1079.17,
                    "series": _series(
                        [
                            3120.91, 3035.90, 2949.12, 2860.54, 2770.11,
                            2677.80, 2583.56, 2487.36, 2389.16, 2288.92,
                            2186.58, 2082.12, 1975.48, 1866.62, 1755.49,
                            1642.05, 1526.25, 1408.03, 1287.35, 1164.16,
                            1038.40, 910.03, 778.98, 645.20, 508.64,
                            369.23, 226.92, 81.65, 0.0,
                        ]
                    ),
                },
                {
                    "monthly_payment": 250.00,
                    "months": 16,
                    "total_interest": 561.95,
                    "series": _series(
                        [
                            3020.91, 2833.82, 2642.83, 2447.87, 2248.85,
                            2045.68, 1838.28, 1626.56, 1410.43, 1189.81,
                            964.58, 734.67, 499.97, 260.38, 15.81, 0.0,
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


@app.get("/api/dashboard", response_model=DashboardResponse)
def dashboard() -> dict:
    return MOCK_DASHBOARD
