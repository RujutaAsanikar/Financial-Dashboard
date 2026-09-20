import { Repeat2 } from 'lucide-react';
import Card from '../components/Card';
import { CardSkeleton, CardError, CardEmpty } from '../components/CardStates';
import { Badge } from '../components/ui/badge';
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from '../components/ui/table';

const money = (n) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(n);
const dateShort = (iso) => new Date(`${iso}T00:00:00`).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });

export default function SubscriptionsCard({ subscriptions, totals, loading, error }) {
  if (loading) {
    return (
      <Card title="Subscriptions">
        <CardSkeleton className="h-12 w-2/3" />
        <CardSkeleton className="mt-4 h-44" />
      </Card>
    );
  }
  if (error) return <Card title="Subscriptions"><CardError message={error} /></Card>;
  if (!subscriptions?.length || !totals) {
    return (
      <Card title="Subscriptions">
        <CardEmpty message="No recurring charges were found." />
      </Card>
    );
  }

  // subscriptions arrive pre-sorted by annual_cost descending — render in order

  return (
    <Card className="relative overflow-hidden border-primary/20 bg-gradient-to-br from-secondary/50 via-card to-card">
      <div className="pointer-events-none absolute -right-16 -top-16 size-56 rounded-full bg-primary/10 blur-3xl" aria-hidden="true" />

      <div className="relative flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-primary">
        <Repeat2 className="size-3.5" />
        The hero feature
      </div>

      <div className="relative mt-1 flex flex-wrap items-baseline gap-x-2.5 gap-y-1 leading-none">
        <span className="tabular text-4xl font-bold text-foreground sm:text-5xl">{totals.count}</span>
        <span className="text-base text-muted-foreground sm:text-lg">subscriptions ·</span>
        <span className="tabular text-4xl font-bold text-primary sm:text-5xl">{money(totals.annual_cost)}</span>
        <span className="text-base text-muted-foreground sm:text-lg">/year ·</span>
        <span className="tabular text-4xl font-bold text-destructive sm:text-5xl">{totals.price_increases}</span>
        <span className="text-base text-muted-foreground sm:text-lg">raised their price</span>
      </div>

      <div className="relative mt-5 overflow-x-auto rounded-xl border border-border bg-background/60">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Merchant</TableHead>
              <TableHead className="text-right">Monthly</TableHead>
              <TableHead className="text-right">Annual</TableHead>
              <TableHead className="text-right">Next charge</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {subscriptions.map((s) => (
              <TableRow key={s.merchant}>
                <TableCell className="max-w-[220px] truncate whitespace-normal font-medium text-foreground">
                  <span className="inline-flex flex-wrap items-center gap-2">
                    {s.merchant}
                    {s.price_change_pct != null && (
                      <Badge variant="destructive" className="tabular">
                        +{s.price_change_pct}%
                      </Badge>
                    )}
                  </span>
                </TableCell>
                <TableCell className="tabular text-right text-muted-foreground">{money(s.amount)}</TableCell>
                <TableCell className="tabular text-right font-semibold text-foreground">{money(s.annual_cost)}</TableCell>
                <TableCell className="tabular text-right text-muted-foreground">{dateShort(s.next_expected)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </Card>
  );
}
