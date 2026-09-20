import { ShieldCheck, TriangleAlert, Repeat2 } from 'lucide-react';
import { CardSkeleton, CardError } from '../components/CardStates';
import { cn } from '../lib/utils';

const money = (n) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(n);

export default function StatusBadges({ transfersExcluded, extraction, loading, error }) {
  if (loading) return <CardSkeleton className="h-9 w-full max-w-md" />;
  if (error) return <CardError message={error} />;
  if (!extraction || !transfersExcluded) return null;

  const reconciled = extraction.reconciled;

  return (
    <div className="flex flex-wrap items-center gap-2">
      <Pill
        tone={reconciled ? 'success' : 'destructive'}
        icon={reconciled ? ShieldCheck : TriangleAlert}
        label={reconciled ? 'Books reconciled' : `Off by ${money(Math.abs(extraction.delta))}`}
      />
      <Pill
        tone="neutral"
        icon={Repeat2}
        label={`${transfersExcluded.count} transfers excluded (${money(transfersExcluded.total)})`}
      />
      {extraction.rows_needing_review > 0 && (
        <Pill tone="destructive" icon={TriangleAlert} label={`${extraction.rows_needing_review} rows need review`} />
      )}
    </div>
  );
}

function Pill({ tone, icon: Icon, label }) {
  const toneClasses = {
    success: 'bg-success/10 text-success',
    destructive: 'bg-destructive/10 text-destructive',
    neutral: 'bg-secondary text-secondary-foreground',
  }[tone];

  return (
    <span className={cn('tabular inline-flex items-center gap-1.5 rounded-full px-3 py-1.5 text-xs font-medium', toneClasses)}>
      <Icon className="size-3.5" />
      {label}
    </span>
  );
}
