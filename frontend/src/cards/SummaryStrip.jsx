import { ArrowDownRight, ArrowUpRight, TrendingUp, TrendingDown, Receipt } from 'lucide-react';
import Card from '../components/Card';
import { CardSkeleton, CardError } from '../components/CardStates';
import { cn } from '../lib/utils';

const money = (n) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(n);

export default function SummaryStrip({ summary, loading, error }) {
  if (loading) {
    return (
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {[1, 2, 3, 4].map((i) => (
          <Card key={i} className="min-h-[104px]">
            <CardSkeleton className="h-14" />
          </Card>
        ))}
      </div>
    );
  }
  if (error) return <Card title="Summary"><CardError message={error} /></Card>;
  if (!summary) return <Card title="Summary"><CardError message="No summary data yet." /></Card>;

  const netPositive = summary.net >= 0;

  const stats = [
    { label: 'Total spent', value: money(summary.total_spent), icon: ArrowDownRight, tone: 'text-destructive' },
    { label: 'Total income', value: money(summary.total_income), icon: ArrowUpRight, tone: 'text-success' },
    {
      label: 'Net',
      value: money(summary.net),
      icon: netPositive ? TrendingUp : TrendingDown,
      tone: netPositive ? 'text-success' : 'text-destructive',
    },
    { label: 'Transactions', value: summary.transaction_count.toLocaleString(), icon: Receipt, tone: 'text-primary' },
  ];

  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      {stats.map((s) => (
        <Card key={s.label} className="min-h-0 gap-2 py-4">
          <div className="flex items-center justify-between">
            <span className="text-xs font-medium text-muted-foreground">{s.label}</span>
            <s.icon className={cn('size-4', s.tone)} />
          </div>
          <span className={cn('tabular text-xl font-semibold tracking-tight sm:text-2xl', s.tone)}>{s.value}</span>
        </Card>
      ))}
    </div>
  );
}
