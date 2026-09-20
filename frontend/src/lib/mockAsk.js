const money = (n) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(n);

// Local stand-in for POST /api/ask when running against mock data — answers
// the four suggested demo questions from the loaded dashboard JSON so the
// Q&A box is demoable with no backend running. Real question answering
// (and SQL generation) happens server-side once VITE_USE_MOCK=false.
export function mockAsk(question, data) {
  const q = (question || '').toLowerCase();

  if (q.includes('food')) {
    const cat = data.by_category?.find((c) => /food/i.test(c.category));
    if (cat) {
      return {
        answer: `You spent ${money(cat.amount)} on ${cat.category} across ${cat.count} transactions in this statement period.`,
        sql: `SELECT category, SUM(amount) AS total, COUNT(*) AS txns\nFROM transactions\nWHERE category = '${cat.category}'\nGROUP BY category;`,
        rows: [{ category: cat.category, total: cat.amount, transactions: cat.count }],
      };
    }
  }

  if (q.includes('recurring') || q.includes('subscription')) {
    const top = data.subscriptions?.[0];
    if (top) {
      return {
        answer: `Your biggest recurring charge is ${top.merchant} at ${money(top.amount)}/month (${money(top.annual_cost)}/year).`,
        sql: `SELECT merchant, amount, annual_cost\nFROM subscriptions\nORDER BY annual_cost DESC\nLIMIT 1;`,
        rows: [{ merchant: top.merchant, monthly: top.amount, annual: top.annual_cost }],
      };
    }
  }

  if (q.includes('weekend')) {
    const total = data.summary?.total_spent ?? 0;
    const weekend = total * 0.29;
    return {
      answer: `You spent about ${money(weekend)} on weekends (Fri–Sun), roughly 29% of your ${money(total)} total spend this period.`,
      sql: `SELECT SUM(amount) AS weekend_spend\nFROM transactions\nWHERE strftime('%w', date) IN ('0','5','6')\n  AND amount < 0;`,
      rows: [{ weekend_spend: Number(weekend.toFixed(2)), share_of_total: '29%' }],
    };
  }

  if (q.includes('compare') || (q.includes('august') && q.includes('july'))) {
    const months = data.spending_over_time ?? [];
    const aug = months.find((m) => m.month?.endsWith('-08'));
    const jul = months.find((m) => m.month?.endsWith('-07'));
    if (aug && jul) {
      const diff = aug.amount - jul.amount;
      const pct = jul.amount ? (diff / jul.amount) * 100 : 0;
      const verb = diff >= 0 ? 'more' : 'less';
      return {
        answer: `You spent ${money(Math.abs(diff))} ${verb} in August (${money(aug.amount)}) than July (${money(jul.amount)}) — a ${Math.abs(pct).toFixed(1)}% ${diff >= 0 ? 'increase' : 'decrease'}.`,
        sql: `SELECT month, SUM(amount) AS total\nFROM transactions\nWHERE month IN ('2026-07', '2026-08')\nGROUP BY month;`,
        rows: [
          { month: jul.month, total: jul.amount },
          { month: aug.month, total: aug.amount },
        ],
      };
    }
  }

  return {
    answer:
      'This demo answers the four suggested questions from the sample data below. Connect the real backend (VITE_USE_MOCK=false) to ask anything and have it generate the SQL live.',
    sql: '-- live SQL generation happens server-side at POST /api/ask',
    rows: [],
  };
}
