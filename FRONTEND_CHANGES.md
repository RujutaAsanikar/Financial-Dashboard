# What changed in the API — frontend update

Three fields were **added** to the response since you started. Nothing was
renamed and nothing was removed, so **your existing code still works.** If you
do nothing, the app keeps rendering exactly as it does today — you just won't
show two new features.

`mock_dashboard.json` is already updated, so you can build against all of this
right now without running the backend.

- Setup / running the backend → [README.md](README.md)
- Full field reference → [API.md](API.md)
- Day-to-day workflow → [FRONTEND.md](FRONTEND.md)

---

## The three changes

| # | What | Where | Effort |
|---|---|---|---|
| 1 | `repeated_spending` + `repeated_spending_totals` | new top-level keys | new section |
| 2 | `label` on each payoff scenario | inside `payoff[].scenarios[]` | one line |
| 3 | `payoff[].scenarios` now has **4** entries, was 2 | inside `payoff[]` | check your chart |

---

## 1. `repeated_spending` — a new section

We split recurring charges into two lists.

**`subscriptions`** is now only things you *pay for* — Netflix, your gym, the
electric bill. Things you could cancel.

**`repeated_spending`** is things you *buy* on a regular rhythm — groceries
every Saturday, coffee every weekday. Regular, but there's nothing to cancel.

Previously both were jumbled into `subscriptions`, which made the subscriptions
table misleading: "you have 11 subscriptions costing $4,227/yr" isn't true if
four of them are the supermarket.

```ts
interface RepeatedSpending {
  merchant: string;
  amount: number;          // typical charge
  cadence_days: number;    // 7 = weekly, 30 = monthly
  annual_cost: number;
  occurrences: number;     // NEW vs Subscription — how many times it happened
  first_seen: string;      // "YYYY-MM-DD"
  last_seen: string;       // "YYYY-MM-DD"
  account_id: string;
}

interface RepeatedSpendingTotals {
  count: number;
  annual_cost: number;
}
```

**It deliberately mirrors `Subscription`**, so if you built a subscriptions
table you can reuse the same component. Two differences:

- `occurrences` instead of `price_change_pct` — the interesting number for a
  habit is *how often*, not whether the price moved
- `last_seen` instead of `next_expected` — we don't predict the next coffee

In the mock: 4 rows, $6,864.14/yr, topped by Giant Eagle at 26 visits.

**Design note:** this reads as an insight, not a leftovers bin. *"$4,505/yr
across 26 grocery trips"* is genuinely useful. Give it its own card near the
subscriptions table, not a footer.

---

## 2. `label` on payoff scenarios

Each scenario now carries a human-readable name:

```ts
interface PayoffScenario {
  label: string;           // NEW — "Minimum only", "Pay off in 3 years", ...
  monthly_payment: number;
  months: number;
  total_interest: number;
  series: { month: number; balance: number }[];
}
```

Use it for the chart legend instead of deriving one from `monthly_payment`.

It's optional in the schema (defaults to `""`) so older payloads still
validate — but the live API always sets it.

---

## 3. Payoff now has four scenarios, not two

This is the one most likely to look wrong if you ignore it. If your chart
assumes two lines, or has a two-colour legend, it needs widening.

| Label | Payment | Months | Interest |
|---|---|---|---|
| Minimum only | $98.77 | 55 | $2,190.54 |
| Pay off in 3 years | $127.39 | 36 | $1,381.34 |
| Minimum + $100 | $198.77 | 20 | $740.60 |
| Pay off in 1 year | $304.53 | 12 | $450.09 |

They're **sorted by payment ascending**, and every `series` ends at balance 0.

**The first two are not arbitrary.** Since the CARD Act, every US credit card
statement is legally required to print exactly this comparison: what the
minimum payment costs you, and what clearing the balance in three years costs
instead. So the chart reproduces a disclosure the bank already made — worth
saying out loud in the demo.

**Where the story is:** the gap between "Minimum only" and "Minimum + $100" is
$1,450 and 35 months. Make that difference visible — an annotation, or
highlighting the two lines — rather than leaving four equal-weight lines for
the viewer to interpret.

Scenarios that never pay off are **omitted**, not returned as null, so
`scenarios.length` can be fewer than 4 on other cards. Don't index by position.

---

## What has NOT changed

- Every existing field name and type
- `subscriptions`, `subscription_totals`, `summary`, `by_category`,
  `spending_over_time`, `accounts`, `transfers_excluded`, `extraction`
- Amounts are still pre-flipped to positive; arrays still pre-sorted

---

## Two things to know that aren't new, but bite

**`closing_balance` means opposite things depending on account type.** On
checking it's money you have; on a credit card it's money you **owe**. Both are
positive. `3204.18` on the Chase card means owing $3,204.18, so don't render it
next to a green up-arrow. Branch on `account_type` and label it "Owed" vs
"Available". See [API.md](API.md) §6 convention 6.

**`subscriptions` may be empty on real uploads.** Detection needs 3+ charges
from the same merchant, so a single one-month statement legitimately returns
`[]`. Your empty state will be seen. Same for `repeated_spending`.

---

## Prompt for Claude

Paste this. It assumes Claude can read your frontend repo.

````
I'm building the React frontend for a personal-finance dashboard. The backend
API added three things and I need to update the UI. Nothing was renamed or
removed, so existing code still works — this is purely additive.

Here's what's new:

1. A new top-level key `repeated_spending` (array) and
   `repeated_spending_totals` ({count, annual_cost}).

   The backend now splits recurring charges in two. `subscriptions` is things
   you PAY FOR and could cancel (Netflix, gym, electric bill).
   `repeated_spending` is things you BUY on a regular rhythm (groceries every
   Saturday, coffee every weekday) — regular, but nothing to cancel.

   RepeatedSpending fields: merchant (string), amount (number, the typical
   charge), cadence_days (number, 7 = weekly), annual_cost (number),
   occurrences (number, how many times it happened), first_seen (string
   "YYYY-MM-DD"), last_seen (string "YYYY-MM-DD"), account_id (string).

   It deliberately mirrors the existing Subscription shape so the table
   component can be reused. Differences: it has `occurrences` and `last_seen`
   where Subscription has `price_change_pct` and `next_expected`.

2. Each entry in `payoff[].scenarios[]` gained a `label` string —
   "Minimum only", "Pay off in 3 years", "Minimum + $100", "Pay off in 1 year".
   Use it for the chart legend instead of deriving a name from
   monthly_payment.

3. `payoff[].scenarios` now contains up to FOUR scenarios; it used to be two.
   They arrive sorted by monthly_payment ascending. Scenarios that never pay
   off are omitted entirely, so the array can be shorter than 4 — never index
   by position, always read the label.

What I want:

- Add a "Repeated spending" section. Reuse the subscriptions table component
  if that's clean; show occurrences prominently since "26 grocery trips" is
  the interesting number for a habit. Include the totals.
- Widen the payoff chart to handle four lines, using `label` in the legend.
- Emphasise the gap between "Minimum only" and "Minimum + $100" — that's
  $1,450 and 35 months apart, and it's the point of the chart. An annotation
  or visual weighting, not four equal lines.
- Handle these states: `repeated_spending` empty, `scenarios` with fewer than
  four entries, and `subscriptions` empty (all legitimate).

Constraints:
- Don't rename or remove anything that already exists — the backend contract
  is frozen and other code depends on it.
- Match the existing styling and component patterns in this repo rather than
  introducing a new approach.
- `amount`, `annual_cost` and `total_interest` are already positive numbers.
  Don't apply Math.abs().

Test data: `mock_dashboard.json` already contains all of this — 4 rows of
repeated spending totalling $6,864.14/yr, and 4 payoff scenarios. Build
against it.

Start by reading my existing subscriptions table and payoff chart components
and tell me your plan before writing code.
````

That last line matters — it'll show you the plan before it edits anything.

---

## Checking your work

```bash
# From the backend repo
.venv/bin/uvicorn main:app --reload
curl -s localhost:8000/api/dashboard | python -m json.tool | head -40
```

Or just use `mock_dashboard.json` — it's identical to what the endpoint returns.

- [ ] Repeated spending section shows 4 rows, $6,864.14/yr total
- [ ] Payoff chart shows 4 labelled lines
- [ ] Each payoff line ends at zero
- [ ] Credit card balance is labelled "Owed", not shown as an asset
- [ ] Empty states render for `repeated_spending: []` and `subscriptions: []`

Anything looks wrong or a number seems off — ask rather than working around
it. A backend fix is cheap; a frontend workaround outlives the hackathon.
