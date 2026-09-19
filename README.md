# Financial-Dashboard

Backend for the personal-finance dashboard. Ingests parsed bank-statement JSON
and serves analytics to the React frontend.

**Frontend devs:** this page gets you running. For what the API actually
returns — every field, TypeScript types, conventions — see **[API.md](API.md)**.

---

## Do you even need to run this?

Probably not yet. `mock_dashboard.json` in this repo is an exact capture of what
`GET /api/dashboard` returns, so you can build the entire UI with no Python:

```ts
import mock from "../mock_dashboard.json";
const data: DashboardResponse = mock;
```

Run the backend when you're ready to swap that for a real `fetch`.

---

## Setup

You need **Python 3.11 or newer** (tested on 3.11 and 3.13) and git. Check with:

```bash
python3 --version
```

If that prints 3.11+ you're set — use `python3` everywhere below. No Gemini API
key is needed; the AI features aren't wired up yet.

### macOS / Linux

```bash
git clone https://github.com/RujutaAsanikar/Financial-Dashboard.git
cd Financial-Dashboard

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

.venv/bin/uvicorn main:app --reload --port 8000
```

### Windows (PowerShell)

```powershell
git clone https://github.com/RujutaAsanikar/Financial-Dashboard.git
cd Financial-Dashboard

py -m venv .venv
.venv\Scripts\pip install -r requirements.txt

.venv\Scripts\uvicorn main:app --reload --port 8000
```

Note the `\Scripts\` vs `/bin/` difference — that's the usual Windows tripwire.

You don't have to "activate" the venv. Calling `.venv/bin/<tool>` directly does
the same thing with less ceremony. If you'd rather activate it
(`source .venv/bin/activate`, or `.venv\Scripts\Activate.ps1`), then plain
`uvicorn main:app --reload` works too.

Leave that last command running — it holds the terminal. Open a second tab for
anything else. `--reload` restarts the server when files change.

---

## Check it worked

```bash
curl localhost:8000/api/health
# {"ok":true}

curl localhost:8000/api/dashboard
```

Or just open **http://localhost:8000/docs** in a browser — interactive API
explorer, click any endpoint and hit "Try it out".

---

## What's available

| Method | Path | Returns |
|---|---|---|
| `GET` | `/api/health` | `{"ok": true}` |
| `GET` | `/api/dashboard` | The whole dashboard payload |

One request gets you everything — no per-widget endpoints, no pagination.

**The data is currently hardcoded mock data** in the final, frozen shape. Build
against it; real data drops in later with no frontend change. More endpoints
(`/api/upload`, `/api/transactions`, `/api/ask`) land in Stage 9 — see
[API.md](API.md) §2, and don't build against those yet, their shapes aren't
frozen.

CORS allows **any** `localhost` / `127.0.0.1` port, so your dev server's port
doesn't matter. Serving from a LAN IP or a tunnel instead? Ask and we'll
whitelist that origin.

---

## When it doesn't work

**`python3: command not found`** — install Python 3.11+ from
[python.org](https://python.org), or `brew install python@3.11` on macOS.

**`python3 --version` says 3.10 or older** — you need 3.11+. Install a newer
one, then use `python3.11 -m venv .venv` explicitly.

**`ModuleNotFoundError: No module named 'fastapi'`** — you're running system
Python instead of the venv. Use the full `.venv/bin/uvicorn` path, or activate
the venv first.

**`Address already in use`** — something's on port 8000. Use another:
`.venv/bin/uvicorn main:app --reload --port 8001`. Update your frontend's base
URL to match.

**CORS error in the browser console** — you're not on a `localhost` origin.
Send us the exact origin and we'll add it.

**`no tests ran`** — expected. Tests arrive in Stage 1.

**Blank page at `/docs`** — Swagger loads from a CDN, so it needs internet. The
API itself works offline; `curl` still gets you real responses.

---

## Repo layout

Only `main.py` and `models.py` matter to the frontend. `models.py` is the frozen
contract — the source of truth for every shape in [API.md](API.md).

The rest (`adapter.py`, `validate.py`, `normalize.py`, `analyze.py`, …) is the
analysis pipeline, built stage by stage per `CLAUDE.md`. Most are still empty
stubs.
