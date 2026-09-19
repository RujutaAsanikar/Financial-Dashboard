# Finance Dashboard — Backend API

Handoff doc for the frontend. Covers how to run the API, what every field
means, and the conventions that will bite you if you don't know them.

**Status: Stage 0.** `/api/dashboard` currently returns *hardcoded mock data*
in the exact final shape. The contract below is frozen — build against it now
and the real data will drop in without a frontend change.

---

## 1. Running the backend

From the backend repo root:

```bash
python3.11 -m venv .venv
.venv/bin/pip install fastapi uvicorn duckdb pandas sqlglot pytest google-genai python-multipart
.venv/bin/uvicorn main:app --reload --port 8000
```

The venv already exists if you cloned after Stage 0 — just run the last line.

| | |
|---|---|
| Base URL | `http://localhost:8000` |
| Interactive docs | `http://localhost:8000/docs` |
| Raw OpenAPI spec | `http://localhost:8000/openapi.json` |

**CORS allows any `localhost` / `127.0.0.1` port**, so your dev server's port
doesn't matter. If you serve the frontend from a LAN IP or a tunnel domain,
tell me and I'll whitelist that origin — it'll fail with a CORS error otherwise.

### No backend? Use the snapshot

`mock_dashboard.json` in the repo root is a byte-for-byte capture of what
`GET /api/dashboard` currently returns. Import it directly and build the whole
UI with no Python installed:

```ts
import mock from "../mock_dashboard.json";
const data: DashboardResponse = mock;
```

Swap it for a real `fetch` at integration time. It's a snapshot of the mock, so
it goes stale if the contract changes — I'll regenerate it when that happens.

### Generating a typed client

`/openapi.json` is a valid OpenAPI 3.1 spec, so you can skip hand-writing
types if you prefer:

```bash
npx openapi-typescript http://localhost:8000/openapi.json -o src/api-types.ts
```

Otherwise copy the TypeScript in section 5 — it's kept in sync by hand.

---

## 2. Endpoints

### Live now

| Method | Path | Returns |
|---|---|---|
| `GET` | `/api/health` | `{"ok": true}` |
| `GET` | `/api/dashboard` | `DashboardResponse` — the whole payload |

```bash
curl -s localhost:8000/api/health
curl -s localhost:8000/api/dashboard | python -m json.tool
```

One request gets you the entire dashboard. There is no per-widget endpoint and
no pagination — the payload is a few hundred KB at most, single-user.

### Coming in Stage 9 / 10

Listed so you can plan routing; **do not build against these yet**, the shapes
aren't frozen.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/upload` | multipart: `file` (parser JSON) + optional `apr`, `credit_limit`, `account_nickname`. Returns the same `DashboardResponse`. |
| `GET` | `/api/transactions` | `?limit=100&category=&account_id=` — the raw transaction list |
| `POST` | `/api/reset` | Wipes the DB. For resetting between demo runs. |
| `POST` | `/api/ask` | `{"question": "..."}` → natural-language Q&A |

Note that `/api/upload` returns the full dashboard, so after an upload you can
render directly from the response instead of re-fetching.

---

## 3. The schema

Swagger lists 13 schemas alphabetically and flat, which hides the structure.
There's really only **one**: `DashboardResponse` is the entire response and
everything else is a nested piece of it. (`HealthResponse` is unrelated — it's
the one-field `/api/health` body.)

```
DashboardResponse
├── accounts            → Account[]
├── summary             → Summary
├── by_category         → CategoryAmount[]
├── spending_over_time  → MonthAmount[]
├── subscriptions       → Subscription[]
├── subscription_totals → SubscriptionTotals
├── payoff              → Payoff[]
│                            └── scenarios → PayoffScenario[]
│                                               └── series → PayoffPoint[]
├── transfers_excluded  → TransfersExcluded
└── extraction          → Extraction
```

Only `payoff` nests more than one level deep. Everything else is a flat record
or a list of flat records.

### What each block is for

| Block | Suggested UI |
|---|---|
| `accounts` | Account cards / account switcher |
| `summary` | Headline KPI row |
| `by_category` | Category donut or bar chart |
| `spending_over_time` | Monthly trend line |
| `subscriptions` | **The hero table** — this is what the demo is built around |
| `subscription_totals` | "You spend $X/yr on subscriptions" stat |
| `payoff` | Amortization curves |
| `transfers_excluded` | "We excluded N transfers" trust badge |
| `extraction` | "Reconciled ✓" trust badge |

The last two are small but they're the point: they make the deterministic
verification work *visible*. Worth real estate on screen, not a footer.

---

## 4. Field reference

### `accounts` — `Account[]`

| Field | Type | Notes |
|---|---|---|
| `id` | `string` | Slug, e.g. `"chase-4821"`. Join key for `subscriptions.account_id` and `payoff.account_id`. |
| `bank_name` | `string \| null` | Null if the statement didn't print it |
| `account_holder_name` | `string \| null` | Name as printed on the statement. Null if absent. |
| `account_last4` | `string \| null` | **Last 4 digits only.** The full number is never stored or returned. |
| `account_type` | `string` | Always one of `checking` / `savings` / `credit` / `unknown` |
| `closing_balance` | `number \| null` | On a credit card this is the amount *owed* |
| `apr` | `number \| null` | Percent, e.g. `24.99`. Null on non-credit accounts. |
| `transaction_count` | `number` | Rows for this account |

### `summary` — `Summary`

| Field | Type | Notes |
|---|---|---|
| `total_spent` | `number` | **Positive.** See sign convention below. |
| `total_income` | `number` | Positive |
| `net` | `number` | `total_income - total_spent`. The one field that can go negative. |
| `transaction_count` | `number` | Excludes transfers |
| `period_start` | `string` | `"YYYY-MM-DD"` |
| `period_end` | `string` | `"YYYY-MM-DD"` |

### `by_category` — `CategoryAmount[]`

Pre-sorted by `amount` descending, so render in array order. Amounts are
positive. `category` is always one of exactly 15 strings (see section 5).

### `spending_over_time` — `MonthAmount[]`

Pre-sorted ascending. `month` is `"YYYY-MM"`. Amounts positive.

### `subscriptions` — `Subscription[]`

Pre-sorted by `annual_cost` descending.

| Field | Type | Notes |
|---|---|---|
| `merchant` | `string` | Normalized name, e.g. `"Netflix.Com"` |
| `amount` | `number` | Positive. Typical amount of a *single* charge. |
| `cadence_days` | `number` | How often it bills — see below |
| `annual_cost` | `number` | `amount × (365 / cadence_days)` |
| `price_change_pct` | `number \| null` | **Null is meaningful** — see below |
| `first_seen` | `string` | `"YYYY-MM-DD"` of the earliest charge |
| `next_expected` | `string` | `"YYYY-MM-DD"` — last charge + cadence |
| `account_id` | `string` | Joins to `accounts.id` |

**`cadence_days`** is the detected billing interval, snapped to one of `7`
(weekly), `14` (biweekly), `30` (monthly), `90` (quarterly), `365` (annual).
A subscription that really bills every 31 days still reports `30`.

It's what makes `annual_cost` comparable across different billing rhythms, and
that comparison is a genuinely good thing to surface. From the current mock:

| Merchant | `amount` | `cadence_days` | `annual_cost` |
|---|---|---|---|
| Netflix.Com | $17.99 | 30 | **$218.88** |
| Nytimes | $4.25 | 7 | **$221.61** |

The NYT charge looks trivial next to Netflix — four times cheaper per charge —
but it bills weekly, so it quietly costs *more* per year. Sorting the table by
per-charge `amount` would bury that. Sort by `annual_cost` (already done).

**`price_change_pct`** is the percent change from first charge to most recent,
e.g. `12.5` for a subscription that went from $15.99 to $17.99. It is set to
`null` when the change is under 2% in absolute terms — meaning **`null` is a
positive finding ("price is stable"), not missing data.** Render it as "—",
not as an error or "data unavailable". The non-null rows are the price hikes
worth highlighting in red.

### `subscription_totals` — `SubscriptionTotals`

`count`, `annual_cost` (sum across all subscriptions), and `price_increases`
(how many have a non-null `price_change_pct`).

### `payoff` — `Payoff[]`

One entry per credit account that has an APR. **Can be an empty array** — a
user with only a checking account gets `[]`. Handle that.

Each entry has `account_id`, `balance`, `apr`, and `scenarios`.

**`scenarios`** are competing repayment plans for that same balance — two of
them, a baseline and baseline + $100/month. Each answers: at this monthly
payment, how long until the card is paid off and what does the interest cost?

From the current mock ($3,204.18 at 24.99% APR):

| `monthly_payment` | `months` | `total_interest` |
|---|---|---|
| $150 | 29 | $1,079.17 |
| $250 | 16 | $561.95 |

Paying $100 more per month clears the card 13 months sooner and saves $517.22
in interest. **That contrast is the feature** — one number the user can act on.
Consider showing the delta explicitly rather than making them subtract.

**`series`** is one `PayoffPoint` per month (`{month: 1, balance: 3120.91}`,
`{month: 2, balance: 3035.90}`, …) tracing the balance down to zero. It exists
so you can draw both curves on one chart — the steep line and the shallow one.
`series.length === months`, and the final balance is always `0.0`.

### `transfers_excluded` — `TransfersExcluded`

`count` and `total` (summed absolute value) of transactions identified as
transfers between the user's own accounts — e.g. paying a credit card from
checking. These are **excluded** from `summary`, `by_category` and
`spending_over_time` so a card payment isn't double-counted as both spending
and income. Surfacing this number is what proves we did it.

### `extraction` — `Extraction`

| Field | Type | Notes |
|---|---|---|
| `reconciled` | `boolean` | Did the extracted transactions match the statement's own arithmetic? |
| `delta` | `number` | Dollar discrepancy. `0.0` on a clean statement. |
| `rows_needing_review` | `number` | Count of rows whose running balance didn't check out |

`reconciled: true` is a green badge. `false` with a non-zero `delta` should be
a visible warning — it means the PDF extraction may have missed or garbled a
row, and the totals shouldn't be fully trusted.

---

## 5. TypeScript types

Copy-paste ready. Matches `models.py` exactly.

```ts
export type Category =
  | "Food & Drink" | "Groceries" | "Transportation" | "Shopping"
  | "Entertainment" | "Subscriptions" | "Utilities" | "Housing"
  | "Health" | "Education" | "Travel" | "Income" | "Transfer"
  | "Fees & Interest" | "Other";

export type AccountType = "checking" | "savings" | "credit" | "unknown";

export interface Account {
  id: string;
  bank_name: string | null;
  account_holder_name: string | null;
  account_last4: string | null;
  account_type: AccountType;   // plain `string` in the OpenAPI schema
  closing_balance: number | null;
  apr: number | null;
  transaction_count: number;
}

export interface Summary {
  total_spent: number;   // positive
  total_income: number;  // positive
  net: number;           // may be negative
  transaction_count: number;
  period_start: string;  // "YYYY-MM-DD"
  period_end: string;    // "YYYY-MM-DD"
}

export interface CategoryAmount {
  category: Category;    // plain `string` in the OpenAPI schema
  amount: number;        // positive
  count: number;
}

export interface MonthAmount {
  month: string;         // "YYYY-MM"
  amount: number;        // positive
}

export interface Subscription {
  merchant: string;
  amount: number;              // positive, per charge
  cadence_days: number;        // 7 | 14 | 30 | 90 | 365
  annual_cost: number;
  price_change_pct: number | null;  // null = stable, NOT unknown
  first_seen: string;          // "YYYY-MM-DD"
  next_expected: string;       // "YYYY-MM-DD"
  account_id: string;
}

export interface SubscriptionTotals {
  count: number;
  annual_cost: number;
  price_increases: number;
}

export interface PayoffPoint {
  month: number;   // ordinal: 1, 2, 3... NOT a calendar month
  balance: number;
}

export interface PayoffScenario {
  monthly_payment: number;
  months: number;
  total_interest: number;
  series: PayoffPoint[];   // length === months, last balance === 0
}

export interface Payoff {
  account_id: string;
  balance: number;
  apr: number;
  scenarios: PayoffScenario[];
}

export interface TransfersExcluded {
  count: number;
  total: number;
}

export interface Extraction {
  reconciled: boolean;
  delta: number;
  rows_needing_review: number;
}

export interface DashboardResponse {
  accounts: Account[];
  summary: Summary;
  by_category: CategoryAmount[];
  spending_over_time: MonthAmount[];
  subscriptions: Subscription[];
  subscription_totals: SubscriptionTotals;
  payoff: Payoff[];
  transfers_excluded: TransfersExcluded;
  extraction: Extraction;
}
```

---

## 6. Conventions that will bite you

**1. Signs are already flipped for display.** Internally every transaction has
a signed amount (negative = money out), but in this API `summary.total_spent`
and every `by_category.amount` are **positive**. You should never need
`Math.abs()` — if you do, something upstream is wrong, tell me. The only field
that legitimately goes negative is `summary.net`.

**2. Every date is a string, never a Date.** `period_start`, `first_seen`,
`next_expected` are `"YYYY-MM-DD"`; `MonthAmount.month` is `"YYYY-MM"`. This is
deliberate — JSON has no date type, and pinning the format in the contract
means you never have to guess at parsing.

**3. `month` means two different things.** `MonthAmount.month` is a calendar
string (`"2026-03"`). `PayoffPoint.month` is an ordinal counter (`1, 2, 3…`
months from today). Same name, different type, same payload. Known wart —
flagged so nobody rediscovers it at 3am.

**4. Sorted arrays are pre-sorted.** `by_category` (by amount desc),
`spending_over_time` (by month asc) and `subscriptions` (by annual_cost desc)
arrive in display order. Don't re-sort unless the user clicks a column.

**5. Empty arrays are normal.** `payoff` is `[]` when there's no credit account
with an APR. `subscriptions` is `[]` before enough history accumulates —
recurring detection needs 3+ occurrences of a merchant.

---

## 7. Backend file map

Only `main.py` and `models.py` matter to the frontend; the rest is listed so
you know where things live if you need to look.

| Path | Purpose |
|---|---|
| `main.py` | FastAPI app + all routes |
| `models.py` | **The frozen contract.** Source of truth for every shape above. |
| `adapter.py` | Parser JSON → canonical rows |
| `validate.py` | Reconciliation — feeds `extraction` |
| `db.py` | DuckDB schema + dedupe |
| `normalize.py` | Merchant string cleanup |
| `categorize.py` | Category assignment — feeds `by_category` |
| `transfers.py` | Transfer pair matching — feeds `transfers_excluded` |
| `analyze.py` | Recurring detection + payoff — feeds `subscriptions`, `payoff` |
| `aggregate.py` | Assembles the `DashboardResponse` |
| `query.py` | Natural-language Q&A |
| `fixtures/` | Hand-written sample parser output for testing |
| `tests/` | pytest suite |

---

## 8. Contract stability

`models.py` is **frozen**. Fields may be *added* in later stages; nothing will
be renamed or removed. If you need a field that isn't here, ask — adding is
cheap, and it's much cheaper than you shipping a workaround.

**Changes since first publish:**

- `accounts[].account_holder_name` added — name as printed on the statement.

Two more are under consideration:

- `subscriptions[].kind` — `"fixed"` vs `"variable"`, distinguishing a true
  subscription (same amount monthly) from a variable bill like electricity.
  Useful if you want to badge them differently.
- `extraction` currently exposes only a *count* of suspect rows. The backend
  knows *which* rows they are. If the UI wants to highlight them, say so before
  you design around the count.
