import { useState } from 'react';
import { LayoutDashboard, ChartColumn, Repeat2, CreditCard, Sparkles, CircleDollarSign } from 'lucide-react';
import { cn } from '../lib/utils';

export const SECTIONS = [
  { id: 'overview', label: 'Overview', icon: LayoutDashboard },
  { id: 'spending', label: 'Spending', icon: ChartColumn },
  { id: 'subscriptions', label: 'Subscriptions', icon: Repeat2 },
  { id: 'payoff', label: 'Payoff', icon: CreditCard },
  { id: 'ask', label: 'Ask AI', icon: Sparkles },
];

export default function Sidebar() {
  const [active, setActive] = useState('overview');

  const goTo = (id) => {
    setActive(id);
    document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };

  return (
    <>
      {/* Desktop rail */}
      <aside className="fixed inset-y-0 left-0 z-40 hidden w-[72px] flex-col items-center gap-1 border-r border-border bg-sidebar py-4 md:flex">
        <div className="mb-4 flex size-10 items-center justify-center rounded-2xl bg-gradient-to-br from-primary to-chart-2 text-primary-foreground shadow-sm">
          <CircleDollarSign className="size-5" strokeWidth={2.25} />
        </div>

        <nav className="flex flex-1 flex-col items-center gap-1.5">
          {SECTIONS.map((s) => (
            <NavButton key={s.id} section={s} active={active === s.id} onClick={() => goTo(s.id)} />
          ))}
        </nav>
      </aside>

      {/* Mobile bottom tab bar */}
      <nav className="fixed inset-x-0 bottom-0 z-40 flex items-stretch justify-around border-t border-border bg-sidebar/95 backdrop-blur md:hidden">
        {SECTIONS.map((s) => {
          const Icon = s.icon;
          const isActive = active === s.id;
          return (
            <button
              key={s.id}
              type="button"
              onClick={() => goTo(s.id)}
              className={cn(
                'flex flex-1 flex-col items-center gap-0.5 py-2.5 text-[10px] font-medium transition-colors',
                isActive ? 'text-primary' : 'text-muted-foreground'
              )}
            >
              <Icon className="size-5" strokeWidth={isActive ? 2.5 : 2} />
              {s.label}
            </button>
          );
        })}
      </nav>
    </>
  );
}

function NavButton({ section, active, onClick }) {
  const Icon = section.icon;
  return (
    <div className="group relative flex w-full justify-center">
      <button
        type="button"
        onClick={onClick}
        aria-label={section.label}
        className={cn(
          'flex size-11 items-center justify-center rounded-xl transition-colors',
          active
            ? 'bg-secondary text-secondary-foreground'
            : 'text-muted-foreground hover:bg-muted hover:text-foreground'
        )}
      >
        <Icon className="size-[19px]" strokeWidth={active ? 2.4 : 2} />
      </button>
      <span className="pointer-events-none absolute left-full ml-2 top-1/2 -translate-y-1/2 whitespace-nowrap rounded-md bg-popover px-2 py-1 text-xs font-medium text-popover-foreground opacity-0 shadow-md ring-1 ring-border transition-opacity group-hover:opacity-100">
        {section.label}
      </span>
    </div>
  );
}
