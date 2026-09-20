import { Upload, RotateCw } from 'lucide-react';
import ThemeToggle from './ThemeToggle';
import { cn } from '../lib/utils';

function greeting() {
  const h = new Date().getHours();
  if (h < 5) return 'Still up';
  if (h < 12) return 'Good morning';
  if (h < 18) return 'Good afternoon';
  return 'Good evening';
}

const dateShort = (iso) =>
  iso ? new Date(`${iso}T00:00:00`).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' }) : null;

export default function Header({ name, periodStart, periodEnd, onUpload, onRefresh, refreshing }) {
  const firstName = name ? name.split(' ')[0] : null;

  return (
    <header className="sticky top-0 z-30 border-b border-border">
      <div className="mesh-glow glass">
        <div className="mx-auto flex max-w-[1400px] flex-wrap items-center justify-between gap-4 px-5 py-4 sm:px-8">
          <div>
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Ledger</p>
            <h1 className="mt-0.5 text-xl font-semibold tracking-tight text-foreground sm:text-2xl">
              {greeting()}
              {firstName ? `, ${firstName}` : ''}
            </h1>
            {periodStart && periodEnd && (
              <p className="mt-0.5 text-xs text-muted-foreground">
                Showing activity from <span className="tabular">{dateShort(periodStart)}</span> to{' '}
                <span className="tabular">{dateShort(periodEnd)}</span>
              </p>
            )}
          </div>

          <div className="flex items-center gap-2">
            <button
              type="button"
              onClick={onRefresh}
              aria-label="Refresh dashboard"
              title="Refresh"
              className="flex size-9 shrink-0 items-center justify-center rounded-full border border-border bg-card text-foreground transition-colors hover:bg-muted disabled:opacity-50"
              disabled={refreshing}
            >
              <RotateCw className={cn('size-4', refreshing && 'animate-spin')} />
            </button>
            <ThemeToggle />
            <button
              type="button"
              onClick={onUpload}
              className="flex items-center gap-1.5 rounded-full bg-primary px-4 py-2 text-sm font-semibold text-primary-foreground shadow-sm transition-transform hover:brightness-110 active:scale-[0.98]"
            >
              <Upload className="size-4" />
              <span className="hidden sm:inline">Upload statement</span>
              <span className="sm:hidden">Upload</span>
            </button>
          </div>
        </div>
      </div>
    </header>
  );
}
