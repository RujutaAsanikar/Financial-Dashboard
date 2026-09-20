# Building the frontend against Stage 0

The backend returns **realistic fake data in the final, frozen shape.** Build
the entire UI against it now. When real data lands, you change one function and
nothing else.

- Setup / running the backend → **[README.md](README.md)**
- Every field explained, TypeScript types → **[API.md](API.md)**
- This page → how to actually work day to day

---

## Step 1 — Get the data

**Option A: no backend.** `mock_dashboard.json` is in this repo. Copy it into
your project and import it. No Python, no server, no network.

**Option B: run the backend.** Three commands in [README.md](README.md), then
`GET http://localhost:8000/api/dashboard`.

Start with A. Switch to B when you're ready to test real network behaviour —
loading spinners, error states, slow responses.

---

## Step 2 — Add the types

Copy the TypeScript block from [API.md](API.md) §5 into `src/types.ts`. It
covers all 12 models and is verified against the live API.

Or generate it, which stays in sync automatically:

```bash
npx openapi-typescript http://localhost:8000/openapi.json -o src/api-types.ts
```

---

## Step 3 — One file to swap later

This is the important bit. Put **all** backend access behind a single module,
so switching from mock to live is a one-line change instead of a hunt through
twenty components.

```ts
// src/api.ts
import mock from "../mock_dashboard.json";
import type { DashboardResponse } from "./types";

const USE_MOCK = import.meta.env.VITE_USE_MOCK !== "false";
const BASE = import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

export async function getDashboard(): Promise<DashboardResponse> {
  if (USE_MOCK) return mock as DashboardResponse;

  const res = await fetch(`${BASE}/api/dashboard`);
  if (!res.ok) throw new Error(`API ${res.status}`);
  return res.json();
}
```

Then flip between them without touching code:

```bash
# .env.local
VITE_USE_MOCK=false
```

A minimal hook:

```tsx
// src/useDashboard.ts
import { useEffect, useState } from "react";
import { getDashboard } from "./api";
import type { DashboardResponse } from "./types";

export function useDashboard() {
  const [data, setData] = useState<DashboardResponse | null>(null);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    getDashboard().then(setData).catch(setError);
  }, []);

  return { data, error, loading: !data && !error };
}
```

**One request gets the whole dashboard.** No per-widget endpoints, no
pagination. Fetch once at the top and pass pieces down.

---

## Step 4 — Build the screens

Each block maps to one component. These are the exact values in the mock, so
you can confirm a component renders correctly:

| Component | Field | What the mock contains |
|---|---|---|
| Account cards | `accounts` | 2 — Chase credit ($3,204.18), PNC checking ($5,417.62) |
| KPI row | `summary` | spent $12,847.33 · income $14,202.50 · net $1,355.17 · 221 txns |
| Category chart | `by_category` | 11 categories, top is Housing $4,200.00 |
| Trend line | `spending_over_time` | 7 points, `2026-03` → `2026-09` |
| **Subscriptions table** | `subscriptions` | **11 rows, top is Duquesne Light at $1,172.87/yr** |
| Subscriptions stat | `subscription_totals` | 11 subs · $4,227.68/yr · 3 price increases |
| Payoff chart | `payoff` | 1 card, 2 scenarios: 29mo/$1,079.17 and 16mo/$561.95 |
| Transfers badge | `transfers_excluded` | 4 transfers, $1,850.00 excluded |
| Reconciled badge | `extraction` | `reconciled: true`, delta `0.0` |

### Where to spend your design time

**The subscriptions table is the centerpiece.** It's the feature the demo is
built around. The three rows with a non-null `price_change_pct` are the payoff
moment — a subscription whose price quietly went up. Make those visually loud.

**The two badges punch above their size.** `extraction.reconciled` and
`transfers_excluded` are how we show the numbers were verified rather than
guessed by an AI. Small components, but they're the credibility argument — give
them real estate, not a footer.

**The payoff chart tells a story with two lines.** Plot both scenarios' `series`
on one axis so the gap is visible, and state the delta in words: *"paying $100
more per month saves $517.22 and clears it 13 months sooner."*

---

## Step 5 — Design these states now, not later

Cheap today, painful the night before the demo.

**Empty `payoff`.** A user with no credit card gets `[]`. Your payoff section
needs an empty state.

**Empty `subscriptions`.** Detection needs 3+ occurrences of a merchant, so a
single uploaded statement can legitimately return `[]`.

**`price_change_pct: null`.** Means "price is stable" — a *positive* finding,
not missing data. Render `—`, never "N/A" or an error.

**`reconciled: false`.** Means the extraction didn't match the statement's
arithmetic. Needs a visible warning state, not a silently hidden badge.

**Negative `summary.net`.** Spending more than income is normal. Don't assume
positive.

**Long merchant names.** Normalized names can be verbose — `"Giant Eagle
Pittsburgh Pa"`, `"Web Bill Payment - Mastercard"`. Make sure the table
truncates gracefully rather than blowing out the layout.

**`bank_name`, `account_last4`, `account_holder_name` can all be `null`** when a
statement didn't print them.

---

## Step 6 — Things that will trip you up

**Amounts are already positive.** `total_spent` and every `by_category.amount`
come through as positive numbers. You should never need `Math.abs()` — if you
do, tell us, it's a backend bug.

**Dates are strings, never `Date`.** `"YYYY-MM-DD"`, except
`spending_over_time[].month` which is `"YYYY-MM"`. Parse with `new Date(str)`
for display; don't expect ISO timestamps.

**Arrays are pre-sorted.** `by_category` by amount descending,
`spending_over_time` by month ascending, `subscriptions` by annual cost
descending. Render in array order; only re-sort if the user clicks a column.

**`month` means two different things.** `spending_over_time[].month` is a
calendar string (`"2026-03"`). `payoff[].scenarios[].series[].month` is an
ordinal counter (`1, 2, 3…`). Same name, different meaning.

**Don't build against `/api/upload`, `/api/transactions` or `/api/ask` yet.**
They're listed in [API.md](API.md) §2 for planning, but they don't exist and
their shapes aren't frozen.

---

## Step 7 — Integration day

When real endpoints land:

1. Set `VITE_USE_MOCK=false`
2. Start the backend (`README.md`)
3. Upload a statement via `/api/upload`
4. Walk the Step 4 table and confirm each component still renders

Expect real data to be **less tidy** than the mock: fewer subscriptions, messier
merchant names, possibly a failed reconciliation. That's exactly why the states
in Step 5 matter.

---

## If something looks wrong

The contract is frozen — fields may be *added*, never renamed or removed. If a
field seems missing, mislabelled, or a number looks wrong, ask rather than
working around it. A backend fix is cheap; a frontend workaround outlives the
hackathon.
