import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, AreaChart, Area, CartesianGrid } from 'recharts';
import Card from '../components/Card';
import ChartTooltip from '../components/ChartTooltip';
import { CardSkeleton, CardError, CardEmpty } from '../components/CardStates';
import { useTheme } from '../ThemeContext';

const money = (n) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 }).format(n);
const monthLabel = (m) => new Date(`${m}-01T00:00:00`).toLocaleDateString('en-US', { month: 'short' });

export default function SpendingCards({ byCategory, spendingOverTime, loading, error }) {
  const { colors } = useTheme();

  if (loading) {
    return (
      <div className="grid gap-5 lg:grid-cols-2">
        <Card title="Spending by category"><CardSkeleton className="h-[220px]" /></Card>
        <Card title="Spending over time"><CardSkeleton className="h-[220px]" /></Card>
      </div>
    );
  }
  if (error) {
    return (
      <div className="grid gap-5 lg:grid-cols-2">
        <Card title="Spending by category"><CardError message={error} /></Card>
        <Card title="Spending over time"><CardError message={error} /></Card>
      </div>
    );
  }
  if (!byCategory?.length && !spendingOverTime?.length) {
    return (
      <Card title="Spending"><CardEmpty message="No spending data yet." /></Card>
    );
  }

  const topCategories = [...(byCategory ?? [])].sort((a, b) => b.amount - a.amount).slice(0, 8);

  return (
    <div className="grid gap-5 lg:grid-cols-2">
      <Card title="Spending by category" description="Top categories, this period">
        {topCategories.length ? (
          <div className="h-[220px]">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={topCategories} layout="vertical" margin={{ left: 8, right: 12, top: 4 }}>
                <XAxis type="number" hide />
                <YAxis
                  type="category"
                  dataKey="category"
                  width={112}
                  tick={{ fontSize: 12, fill: colors.muted }}
                  axisLine={false}
                  tickLine={false}
                />
                <Tooltip cursor={{ fill: colors.surfaceMuted }} content={<ChartTooltip formatter={(v) => money(v)} />} />
                <Bar dataKey="amount" fill={colors.primary} radius={[0, 8, 8, 0]} barSize={16} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <CardEmpty message="No category data yet." />
        )}
      </Card>

      <Card title="Spending over time" description="Monthly total, this period">
        {spendingOverTime?.length ? (
          <div className="h-[220px]">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={spendingOverTime} margin={{ left: 0, right: 12, top: 4 }}>
                <defs>
                  <linearGradient id="spendGradient" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={colors.primary} stopOpacity={0.35} />
                    <stop offset="100%" stopColor={colors.primary} stopOpacity={0} />
                  </linearGradient>
                </defs>
                <CartesianGrid stroke={colors.border} vertical={false} />
                <XAxis
                  dataKey="month"
                  tickFormatter={monthLabel}
                  tick={{ fontSize: 11, fill: colors.muted }}
                  axisLine={false}
                  tickLine={false}
                />
                <YAxis hide />
                <Tooltip content={<ChartTooltip formatter={(v) => money(v)} />} />
                <Area
                  type="monotone"
                  dataKey="amount"
                  stroke={colors.primary}
                  strokeWidth={2.5}
                  fill="url(#spendGradient)"
                  dot={{ r: 3, fill: colors.primary, strokeWidth: 0 }}
                  activeDot={{ r: 5 }}
                />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <CardEmpty message="No trend data yet." />
        )}
      </Card>
    </div>
  );
}
