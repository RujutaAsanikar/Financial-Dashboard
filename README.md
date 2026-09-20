# Financial-Dashboard

Upload your bank and credit-card statements. Get back a dashboard that tells you
where the money actually went, what's quietly recurring, what got more
expensive, how long the card will take to pay off, and lets you ask questions
about it in plain English.

A model reads the PDF and a model writes SQL. Every
number in between is produced by deterministic Python you can read and test:
reconciliation, transfer matching, recurring detection, amortization. The model
never computes a figure.

| Doc | For |
|---|---|
| This page | What it does, how to run it, how the pipeline works |
| [FRONTEND.md](FRONTEND.md) | Building the UI |
| [API.md](API.md) | Reference: every field, TypeScript types, conventions |
| `CLAUDE.md` | The build spec — stage by stage |

---

## What happens when you upload a statement

```
  statement.pdf
        │
        ▼
  ┌───────────────────┐
  │ 1. EXTRACT        │  Claude reads the PDF/image → parser JSON
  │    raw_extraction │  (skipped if you upload .json directly)
  └───────────────────┘
        │
        ▼
  ┌───────────────────┐
  │ 2. ADAPT          │  → one canonical row shape
  │    adapter.py     │  signs amounts, truncates the account number to 4 digits
  └───────────────────┘
        │
        ▼
  ┌───────────────────┐
  │ 3. RECONCILE      │  Does the extraction agree with the statement's own maths?
  │    validate.py    │  Three checks. This is the "we didn't hallucinate" badge.
  └───────────────────┘
        │
        ▼
  ┌───────────────────┐
  │ 4. NORMALIZE      │  "SQ *COFFEE TREE ROASTERS 04213 PITTSBURGH PA"
  │    normalize.py   │   → "Coffee Tree Roasters"
  └───────────────────┘
        │
        ▼
  ┌───────────────────┐
  │ 5. CATEGORIZE     │  dictionary → cache → Triqai → Claude → "Other"
  │    categorize.py  │  the model only ever sees the tail it can't resolve
  └───────────────────┘
        │
        ▼
  ┌───────────────────┐
  │ 6. STORE          │  DuckDB. Re-uploading the same statement is a no-op.
  │    db.py          │
  └───────────────────┘
        │
        ▼
  ┌───────────────────┐
  │ 7. LINK & DETECT  │  transfers.py  pairs the card payment with the
  │    transfers.py   │                chequing withdrawal, so it isn't
  │    analyze.py     │                counted as both spending and income
  │                   │  analyze.py    finds subscriptions, price rises, payoff
  └───────────────────┘
        │
        ▼
  ┌───────────────────┐
  │ 8. SERVE          │  GET /api/dashboard — one request, whole payload
  │    aggregate.py   │
  └───────────────────┘
```

Steps 7 and 8 run over **everything in the database**, not just the file you
uploaded. A card payment pairs with a withdrawal that may have arrived in a
different upload, and a subscription's history spans statements.

### Why reconciliation matters

Before anything is stored, the extraction is checked against arithmetic the
statement prints about itself:

| Check | Asks |
|---|---|
| `sum_vs_balance` | Do the transactions add up to `closing − opening`? |
| `totals_vs_balance` | Do the statement's own printed totals agree? |
| `running_balance` | Does each row's balance move by exactly that row's amount? |



---

## Using it

### 1. Upload

Drag in one or two files. Accepted: **`.pdf` `.png` `.jpg` `.jpeg` `.gif`
`.webp`** (read by Claude) or **`.json`** (already-parsed output).

Tick **credit card** on a card statement and give its **APR** — that's the only
thing the payoff projection can't learn from the file itself.

> **Upload chequing and credit together, in one go.** Transfer detection pairs
> across accounts, so sending them separately shows your card payments as
> spending until the second file lands.

Each upload **replaces** the dashboard rather than adding to it. Use
`POST /api/reset` (or the reset button) to clear up front.

### 2. Read the dashboard

- **Books reconciled** badge, and how many rows need review
- **Total spent / income / net** — transfers excluded from all three
- **Subscriptions** — cadence, annual cost, and **price increases**
- **Repeated spending** — regular but not a subscription (the weekly coffee)
- **Payoff** — four scenarios, from minimum-only to a year

### 3. Ask questions

Plain English. A model turns the question into **one SQL SELECT**; DuckDB does
the arithmetic; the answer is built only from the rows that came back, with the
SQL shown so you can check the work.

It refuses rather than guesses. Questions asking for advice, a forecast, or
anything the schema doesn't hold come back as *"That isn't answerable from the
statement data available."* Three guards sit on the generated SQL: sqlglot
**parses** it and requires exactly one `SELECT`, it's wrapped in a `LIMIT`, and
the connection is **read-only**.

---

## Running it

You need **Python 3.11+** and **Node 18+**.

```bash
git clone https://github.com/RujutaAsanikar/Financial-Dashboard.git
cd Financial-Dashboard

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

On Windows use `py -m venv .venv` and `.venv\Scripts\pip` — the `\Scripts\`
vs `/bin/` swap is the usual tripwire.

### API key

Reading PDFs, the categorization fallback and the Q&A box all call Claude. Put
the key in a `.env` file at the repo root:

```
ANTHROPIC_API_KEY=sk-ant-...
```

`.env` is gitignored. Without a key the app still runs — upload `.json` instead
of PDFs, unknown merchants land in "Other", and the Q&A box reports that it
couldn't reach the model. Everything else is unaffected, because everything else
is plain Python.

### Start both halves

```bash
# terminal 1 — backend
.venv/bin/uvicorn main:app --reload --port 8000

# terminal 2 — frontend
npm --prefix frontend install
npm --prefix frontend run dev
```

Then open **http://localhost:5173**.

> **The frontend defaults to mock data.** Without this it renders
> `mock_dashboard.json` and the chat box answers from a canned script — it will
> look like it works while ignoring everything you upload. Create
> `frontend/.env.local`:
>
> ```
> VITE_USE_MOCK=false
> VITE_API_BASE=http://localhost:8000
> ```

Check the backend on its own with `curl localhost:8000/api/health` → `{"ok":true}`,
or open **http://localhost:8000/docs** for a clickable API explorer.

### Tests

```bash
.venv/bin/python -m pytest -q
```

Fully offline — no test reaches the network, and `tests/conftest.py` strips API
keys for the run so a stray call fails loudly instead of spending money.

---

## Endpoints

| Method | Path | Does |
|---|---|---|
| `GET` | `/api/health` | liveness |
| `GET` | `/api/dashboard` | the whole payload, one request |
| `POST` | `/api/upload` | one statement (+ `apr`, `account_nickname`) |
| `POST` | `/api/upload-batch` | several at once — **preferred** |
| `GET` | `/api/transactions` | rows, filterable by `category` / `account_id` |
| `POST` | `/api/ask` | `{"question": "..."}` → `{answer, sql, rows}` |
| `GET` | `/api/ask/suggestions` | the demo questions |
| `POST` | `/api/reset` | wipe the database |
| `GET` | `/api/mock-dashboard` | the Stage 0 fixture, for frontend dev |

CORS allows any `localhost` / `127.0.0.1` port.

---

## Repo layout

```
main.py          FastAPI app and routes
models.py        the frozen response contract
data_extraction.py / raw_extraction.py   PDF & image → parser JSON
adapter.py       parser JSON → canonical rows
validate.py      reconciliation
db.py            DuckDB schema, dedupe, persistence
normalize.py     merchant string cleanup
categorize.py    the categorization cascade
transfers.py     cross-account pair matching
analyze.py       recurring detection + payoff amortization
aggregate.py     dashboard assembly
query.py         text-to-SQL and the guards around it
fixtures/demo/   3-month chequing + credit statements to try it with
```

The database is a single file, `finance.duckdb`, gitignored.

---

---

