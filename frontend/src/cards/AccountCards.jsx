import { Landmark, Wallet, CreditCard } from 'lucide-react';
import Card from '../components/Card';
import { CardSkeleton, CardError, CardEmpty } from '../components/CardStates';
import { cn } from '../lib/utils';

const money = (n) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(n);

const TYPE_META = {
  checking: { icon: Landmark, tone: 'text-primary', bg: 'bg-primary/10' },
  savings: { icon: Wallet, tone: 'text-success', bg: 'bg-success/10' },
  credit: { icon: CreditCard, tone: 'text-destructive', bg: 'bg-destructive/10' },
};

export default function AccountCards({ accounts, loading, error }) {
  if (loading) {
    return (
      <Card title="Accounts">
        <div className="flex gap-3 overflow-hidden">
          {[1, 2, 3].map((i) => (
            <CardSkeleton key={i} className="h-20 w-52 shrink-0" />
          ))}
        </div>
      </Card>
    );
  }
  if (error) return <Card title="Accounts"><CardError message={error} /></Card>;
  if (!accounts?.length) return <Card title="Accounts"><CardEmpty message="No accounts linked yet." /></Card>;

  return (
    <Card title="Accounts" description={`${accounts.length} linked`}>
      <div className="-mx-1 flex gap-3 overflow-x-auto px-1 pb-1">
        {accounts.map((a) => {
          const meta = TYPE_META[a.account_type?.toLowerCase()] ?? TYPE_META.checking;
          const Icon = meta.icon;
          const isCredit = a.account_type?.toLowerCase() === 'credit';
          return (
            <div
              key={a.id}
              className="flex min-w-[210px] flex-1 flex-col gap-2 rounded-xl border border-border bg-background p-4"
            >
              <div className="flex items-center justify-between">
                <span className="text-xs font-medium text-muted-foreground">
                  {a.bank_name ?? 'Bank'} {a.account_last4 ? `••${a.account_last4}` : ''}
                </span>
                <span className={cn('flex size-7 items-center justify-center rounded-full', meta.bg)}>
                  <Icon className={cn('size-3.5', meta.tone)} />
                </span>
              </div>
              <span className={cn('tabular text-lg font-semibold', isCredit ? 'text-destructive' : 'text-foreground')}>
                {a.closing_balance != null ? money(a.closing_balance) : '—'}
              </span>
              <span className="text-xs capitalize text-muted-foreground">
                {isCredit ? 'Owed' : 'Available'} · {a.account_type}
                {a.apr != null ? ` · ${a.apr}% APR` : ''}
              </span>
            </div>
          );
        })}
      </div>
    </Card>
  );
}
