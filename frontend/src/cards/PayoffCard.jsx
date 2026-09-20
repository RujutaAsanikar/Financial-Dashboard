import { LineChart, Line, XAxis, YAxis, Tooltip, Legend, ResponsiveContainer, CartesianGrid } from 'recharts';
import { CreditCard } from 'lucide-react';
import Card from '../components/Card';
import ChartTooltip from '../components/ChartTooltip';
import { CardSkeleton, CardError, CardEmpty } from '../components/CardStates';
import { useTheme } from '../ThemeContext';
import { chartPalette } from '../theme';

const money = (n) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 }).format(n);

export default function PayoffCard({ payoffEntries, accounts, loading, error }) {
  if (loading) {
    return (
      <Card title="Payoff projection"><CardSkeleton className="h-[240px]" /></Card>
    );
  }
  if (error) return <Card title="Payoff projection"><CardError message={error} /></Card>;
  if (!payoffEntries?.length) {
    return (
      <Card title="Payoff projection"><CardEmpty message="No credit card balances found." /></Card>
    );
  }

  return (
    <div className="flex flex-col gap-5">
      {payoffEntries.map((entry) => {
        const account = accounts?.find((a) => a.id === entry.account_id);
        const label = account
          ? `${account.bank_name ?? 'Card'}${account.account_last4 ? ` ••${account.account_last4}` : ''}`
          : 'Card';
        return <PayoffEntry key={entry.account_id} entry={entry} label={label} />;
      })}
    </div>
  );
}

// A scenario's own label is the source of truth — never index scenarios by
// array position, since the backend omits ones that never pay off.
const keyOf = (s) => s.label || `$${s.monthly_payment}/mo`;

function PayoffEntry({ entry, label }) {
  const { colors, mode } = useTheme();
  const quietPalette = chartPalette(mode);

  if (!entry.scenarios?.length) {
    return (
      <Card title={`Payoff — ${label}`}><CardEmpty message="No scenarios to project." /></Card>
    );
  }

  const sorted = [...entry.scenarios].sort((a, b) => a.monthly_payment - b.monthly_payment);

  const byLabel = (l) => sorted.find((s) => s.label === l);
  const named = byLabel('Minimum only') && byLabel('Minimum + $100')
    ? [byLabel('Minimum only'), byLabel('Minimum + $100')]
    : null;
  const emphasized = named ?? (sorted.length >= 2 ? [sorted[0], sorted[sorted.length - 1]] : null);

  const maxMonth = Math.max(...sorted.map((s) => s.series.length));
  const merged = Array.from({ length: maxMonth }, (_, i) => {
    const row = { month: i + 1 };
    sorted.forEach((s) => {
      row[keyOf(s)] = s.series[i]?.balance ?? null;
    });
    return row;
  });

  return (
    <Card
      title={`Payoff — ${label}`}
      description={`${money(entry.balance)} owed at ${entry.apr}% APR`}
      action={<CreditCard className="size-4 text-muted-foreground" />}
    >
      <div className="h-[220px]">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={merged} margin={{ left: 0, right: 12, top: 4 }}>
            <CartesianGrid stroke={colors.border} vertical={false} />
            <XAxis
              dataKey="month"
              tick={{ fontSize: 11, fill: colors.muted }}
              axisLine={false}
              tickLine={false}
              label={{ value: 'months', position: 'insideBottom', offset: -4, fontSize: 11, fill: colors.muted }}
            />
            <YAxis hide />
            <Tooltip content={<ChartTooltip formatter={(v) => money(v)} />} />
            <Legend wrapperStyle={{ fontSize: 12 }} />
            {sorted.map((s, i) => {
              const isEmphasized = emphasized?.includes(s);
              const isLow = emphasized && s === emphasized[0];
              if (isEmphasized) {
                return (
                  <Line
                    key={keyOf(s)}
                    type="monotone"
                    dataKey={keyOf(s)}
                    stroke={isLow ? colors.muted : colors.primary}
                    strokeWidth={isLow ? 2 : 2.75}
                    strokeDasharray={isLow ? '4 4' : undefined}
                    dot={false}
                  />
                );
              }
              return (
                <Line
                  key={keyOf(s)}
                  type="monotone"
                  dataKey={keyOf(s)}
                  stroke={quietPalette[i % quietPalette.length]}
                  strokeWidth={1.5}
                  strokeOpacity={0.45}
                  dot={false}
                />
              );
            })}
          </LineChart>
        </ResponsiveContainer>
      </div>

      {emphasized ? (
        <p className="mt-3 rounded-xl bg-success/10 px-4 py-3 text-sm leading-relaxed text-foreground">
          Paying <strong className="tabular">${emphasized[1].monthly_payment}</strong>/month ({keyOf(emphasized[1])})
          instead of <strong className="tabular">${emphasized[0].monthly_payment}</strong>/month ({keyOf(emphasized[0])}) saves
          you{' '}
          <strong className="tabular text-success">
            {money(emphasized[0].total_interest - emphasized[1].total_interest)}
          </strong>{' '}
          and <strong className="tabular text-success">{emphasized[0].months - emphasized[1].months} months</strong>.
        </p>
      ) : (
        <p className="mt-3 rounded-xl bg-secondary/40 px-4 py-3 text-sm leading-relaxed text-foreground">
          At <strong className="tabular">${sorted[0].monthly_payment}</strong>/month, this balance is paid off in{' '}
          <strong className="tabular">{sorted[0].months} months</strong>, costing{' '}
          <strong className="tabular">{money(sorted[0].total_interest)}</strong> in interest.
        </p>
      )}
    </Card>
  );
}
