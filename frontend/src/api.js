import mock from './mock_dashboard.json';
import { mockAsk } from './lib/mockAsk';

// Single place backend access goes through. Flip VITE_USE_MOCK in .env.local
// to switch from mock to the live backend — nothing else changes.
const USE_MOCK = import.meta.env.VITE_USE_MOCK !== 'false';
const BASE = import.meta.env.VITE_API_BASE ?? 'http://localhost:8000';

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export async function getDashboard() {
  if (USE_MOCK) return mock;

  const res = await fetch(`${BASE}/api/dashboard`);
  if (!res.ok) throw new Error(`Server returned ${res.status}`);
  return res.json();
}

// The parser emits JSON, one file per statement, named *_normalized.json.
const ACCEPTED = /\.json$/i;

export async function uploadStatement(file, meta = {}) {
  if (USE_MOCK) {
    await wait(900);
    if (!file || !ACCEPTED.test(file.name)) {
      throw new Error('Please upload a .json file from the parser.');
    }
    return mock;
  }

  const form = new FormData();
  form.append('file', file);
  // Field names must match the backend's form parameters exactly; anything
  // else is silently ignored by FastAPI.
  if (meta.apr != null && meta.apr !== '') form.append('apr', meta.apr);
  if (meta.nickname) form.append('account_nickname', meta.nickname);

  const res = await fetch(`${BASE}/api/upload`, { method: 'POST', body: form });
  if (!res.ok) throw new Error(await errorText(res, 'Upload failed'));
  return res.json();
}

/**
 * Upload several statements in one request.
 *
 * Preferred over looping uploadStatement: transfer detection runs across all
 * accounts, so sending a chequing and a credit statement separately makes the
 * dashboard show the card payment as spending until the second one lands.
 */
export async function uploadStatements(files, meta = {}) {
  if (USE_MOCK) {
    await wait(900);
    const bad = [...files].find((f) => !ACCEPTED.test(f.name));
    if (bad) throw new Error(`${bad.name} is not a .json file from the parser.`);
    return mock;
  }

  const form = new FormData();
  for (const file of files) form.append('files', file);
  if (meta.apr != null && meta.apr !== '') form.append('apr', meta.apr);

  const res = await fetch(`${BASE}/api/upload-batch`, { method: 'POST', body: form });
  if (!res.ok) throw new Error(await errorText(res, 'Upload failed'));
  return res.json();
}

/** FastAPI returns {detail: "..."}; fall back to the raw body or status. */
async function errorText(res, fallback) {
  try {
    const body = await res.json();
    if (typeof body?.detail === 'string') return body.detail;
    if (Array.isArray(body?.detail)) return body.detail.map((d) => d.msg).join('; ');
  } catch {
    /* not JSON */
  }
  return `${fallback} (${res.status})`;
}

export async function askQuestion(question) {
  if (USE_MOCK) {
    await wait(1300);
    return mockAsk(question, mock);
  }

  const res = await fetch(`${BASE}/api/ask`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ question }),
  });

  // /api/ask lands in Stage 10. Until then the live backend 404s, and a raw
  // "Server returned 404" reads like a bug rather than an unbuilt feature.
  if (res.status === 404) {
    return {
      answer:
        "Ask isn't wired to the backend yet — it answers from the sample data " +
        'for now. Set VITE_USE_MOCK=true to try it.',
      sql: '',
      rows: [],
      unavailable: true,
    };
  }
  if (!res.ok) throw new Error(await errorText(res, 'Ask failed'));
  return res.json();
}
