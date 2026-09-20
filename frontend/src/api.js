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

export async function uploadStatement(file, meta = {}) {
  if (USE_MOCK) {
    await wait(900);
    if (!file || !/\.csv$/i.test(file.name)) {
      throw new Error('Please upload a .csv file.');
    }
    return mock;
  }

  const form = new FormData();
  form.append('file', file);
  if (meta.accountType) form.append('account_type', meta.accountType);
  if (meta.nickname) form.append('nickname', meta.nickname);

  const res = await fetch(`${BASE}/api/upload`, { method: 'POST', body: form });
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    throw new Error(text || `Upload failed (${res.status})`);
  }
  return res.json();
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
  if (!res.ok) throw new Error(`Server returned ${res.status}`);
  return res.json();
}
