"""Pydantic v2 contract for the dashboard API.

FROZEN after Stage 0. Adding a field is allowed; renaming or removing one
is not. The frontend is built against this shape.
"""

from pydantic import BaseModel, Field


class Account(BaseModel):
    id: str
    bank_name: str | None = None
    account_holder_name: str | None = None
    account_last4: str | None = None
    account_type: str
    closing_balance: float | None = None
    apr: float | None = None
    transaction_count: int


class Summary(BaseModel):
    total_spent: float
    total_income: float
    net: float
    transaction_count: int
    period_start: str
    period_end: str


class CategoryAmount(BaseModel):
    category: str
    amount: float
    count: int


class MonthAmount(BaseModel):
    month: str
    amount: float


class Subscription(BaseModel):
    merchant: str
    amount: float
    cadence_days: int
    annual_cost: float
    price_change_pct: float | None = None
    first_seen: str
    next_expected: str
    account_id: str


class SubscriptionTotals(BaseModel):
    count: int
    annual_cost: float
    price_increases: int


class RepeatedSpending(BaseModel):
    """A merchant charged on a regular cadence that is NOT a subscription.

    Same detection as `subscriptions` -- these passed every cadence and
    regularity test. They are split out because the customer chooses to buy
    each time rather than having agreed to be billed: coffee on the way to
    work, a weekly grocery run. Cancelling is not a concept here, so they do
    not belong in a "what am I paying for?" list -- but "$340/month across 62
    coffee purchases" is worth seeing on its own.

    Deliberately mirrors Subscription so the frontend can reuse the same
    table component, plus `occurrences`, which is the interesting number for
    a habit in a way it is not for a subscription.
    """

    merchant: str
    amount: float
    cadence_days: int
    annual_cost: float
    occurrences: int
    first_seen: str
    last_seen: str
    account_id: str


class RepeatedSpendingTotals(BaseModel):
    count: int
    annual_cost: float


class PayoffPoint(BaseModel):
    month: int
    balance: float


class PayoffScenario(BaseModel):
    monthly_payment: float
    months: int
    total_interest: float
    series: list[PayoffPoint]


class Payoff(BaseModel):
    account_id: str
    balance: float
    apr: float
    scenarios: list[PayoffScenario]


class TransfersExcluded(BaseModel):
    count: int
    total: float


class Extraction(BaseModel):
    reconciled: bool
    delta: float
    rows_needing_review: int


class DashboardResponse(BaseModel):
    accounts: list[Account]
    summary: Summary
    by_category: list[CategoryAmount]
    spending_over_time: list[MonthAmount]
    subscriptions: list[Subscription]
    subscription_totals: SubscriptionTotals
    # Added after Stage 0. Defaulted so any caller built against the original
    # contract keeps validating -- the freeze permits adding a field, not
    # breaking one.
    repeated_spending: list[RepeatedSpending] = Field(default_factory=list)
    repeated_spending_totals: RepeatedSpendingTotals = Field(
        default_factory=lambda: RepeatedSpendingTotals(count=0, annual_cost=0.0)
    )
    payoff: list[Payoff]
    transfers_excluded: TransfersExcluded
    extraction: Extraction


class HealthResponse(BaseModel):
    ok: bool = Field(default=True)
