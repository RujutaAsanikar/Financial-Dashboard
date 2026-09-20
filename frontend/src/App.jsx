import { useState } from 'react';
import Sidebar from './components/Sidebar';
import Header from './components/Header';
import UploadZone from './components/UploadZone';
import ErrorBoundary from './components/ErrorBoundary';
import { useDashboard } from './useDashboard';
import AccountCards from './cards/AccountCards';
import SummaryStrip from './cards/SummaryStrip';
import SpendingCards from './cards/SpendingCards';
import SubscriptionsCard from './cards/SubscriptionsCard';
import RepeatedSpendingCard from './cards/RepeatedSpendingCard';
import PayoffCard from './cards/PayoffCard';
import StatusBadges from './cards/StatusBadges';
import AskBox from './AskBox';

export default function App() {
  const { data, loading, error, refetch } = useDashboard();
  const [uploadOpen, setUploadOpen] = useState(false);

  const accountHolder = data?.accounts?.[0]?.account_holder_name;

  return (
    <div className="min-h-screen bg-background">
      <Sidebar />

      <div className="md:pl-[72px]">
        <Header
          name={accountHolder}
          periodStart={data?.summary?.period_start}
          periodEnd={data?.summary?.period_end}
          onUpload={() => setUploadOpen(true)}
          onRefresh={refetch}
          refreshing={loading}
        />

        <main className="mx-auto flex max-w-[1400px] flex-col gap-10 px-5 pb-24 pt-6 sm:px-8 md:pb-14">
          <section id="overview" className="flex scroll-mt-24 flex-col gap-5">
            <ErrorBoundary label="Status">
              <StatusBadges
                transfersExcluded={data?.transfers_excluded}
                extraction={data?.extraction}
                loading={loading}
                error={error}
              />
            </ErrorBoundary>
            <ErrorBoundary label="Summary">
              <SummaryStrip summary={data?.summary} loading={loading} error={error} />
            </ErrorBoundary>
            <ErrorBoundary label="Accounts">
              <AccountCards accounts={data?.accounts} loading={loading} error={error} />
            </ErrorBoundary>
          </section>

          <section id="spending" className="flex scroll-mt-24 flex-col gap-4">
            <SectionHeading title="Spending" subtitle="Where the money actually went" />
            <ErrorBoundary label="Spending">
              <SpendingCards
                byCategory={data?.by_category}
                spendingOverTime={data?.spending_over_time}
                loading={loading}
                error={error}
              />
            </ErrorBoundary>
          </section>

          <section id="subscriptions" className="flex scroll-mt-24 flex-col gap-4">
            <SectionHeading title="Subscriptions" subtitle="What's quietly recurring — and what got more expensive" />
            <ErrorBoundary label="Subscriptions">
              <SubscriptionsCard
                subscriptions={data?.subscriptions}
                totals={data?.subscription_totals}
                loading={loading}
                error={error}
              />
            </ErrorBoundary>
            <ErrorBoundary label="Repeated spending">
              <RepeatedSpendingCard
                repeatedSpending={data?.repeated_spending}
                totals={data?.repeated_spending_totals}
                loading={loading}
                error={error}
              />
            </ErrorBoundary>
          </section>

          <section id="payoff" className="flex scroll-mt-24 flex-col gap-4">
            <SectionHeading title="Payoff" subtitle="How fast you could be debt-free" />
            <ErrorBoundary label="Payoff">
              <PayoffCard payoffEntries={data?.payoff} accounts={data?.accounts} loading={loading} error={error} />
            </ErrorBoundary>
          </section>

          <section id="ask" className="flex scroll-mt-24 flex-col gap-4">
            <SectionHeading title="Ask AI" subtitle="Plain-English questions about your own money" />
            <ErrorBoundary label="Ask AI">
              <AskBox />
            </ErrorBoundary>
          </section>
        </main>
      </div>

      <UploadZone open={uploadOpen} onClose={() => setUploadOpen(false)} onUploaded={refetch} />
    </div>
  );
}

function SectionHeading({ title, subtitle }) {
  return (
    <div>
      <h2 className="text-lg font-semibold tracking-tight text-foreground">{title}</h2>
      {subtitle && <p className="text-sm text-muted-foreground">{subtitle}</p>}
    </div>
  );
}
