import Card from '../components/Card';
import { CardSkeleton, CardError, CardEmpty } from '../components/CardStates';
import { Badge } from '../components/ui/badge';
import { Table, TableHeader, TableBody, TableRow, TableHead, TableCell } from '../components/ui/table';

const money = (n) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(n);
const dateShort = (iso) => new Date(`${iso}T00:00:00`).toLocaleDateString('en-US', { month: 'short', day: 'numeric' });

export default function RepeatedSpendingCard({ repeatedSpending, totals, loading, error }) {
  if (loading) {
    return (
      <Card title="Repeated spending">
        <CardSkeleton className="h-6 w-1/2" />
        <CardSkeleton className="mt-4 h-40" />
      </Card>
    );
  }
  if (error) return <Card title="Repeated spending"><CardError message={error} /></Card>;
  if (!repeatedSpending?.length || !totals) {
    return (
      <Card title="Repeated spending">
        <CardEmpty message="No repeated purchases were detected." />
      </Card>
    );
  }

  // repeated_spending arrives pre-sorted by annual_cost descending — render in order

  return (
    <Card
      title="Repeated spending"
      description={`${totals.count} habits · ${money(totals.annual_cost)}/year — regular, but nothing to cancel`}
    >
      <div className="overflow-x-auto rounded-xl border border-border">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Merchant</TableHead>
              <TableHead className="text-right">Typical charge</TableHead>
              <TableHead className="text-right">Annual</TableHead>
              <TableHead className="text-right">Last seen</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {repeatedSpending.map((r) => (
              <TableRow key={r.merchant}>
                <TableCell className="max-w-[220px] truncate whitespace-normal font-medium text-foreground">
                  <span className="inline-flex flex-wrap items-center gap-2">
                    {r.merchant}
                    <Badge variant="secondary" className="tabular">
                      {r.occurrences}×
                    </Badge>
                  </span>
                </TableCell>
                <TableCell className="tabular text-right text-muted-foreground">{money(r.amount)}</TableCell>
                <TableCell className="tabular text-right font-semibold text-foreground">{money(r.annual_cost)}</TableCell>
                <TableCell className="tabular text-right text-muted-foreground">{dateShort(r.last_seen)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </Card>
  );
}
