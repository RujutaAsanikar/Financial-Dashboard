import { TriangleAlert, Inbox } from 'lucide-react';
import { cn } from '../lib/utils';

// Shared loading / error / empty treatments so every card guards against
// null data on first render the same way — this is the pattern that
// prevents a hard refresh from white-screening the dashboard.

export function CardSkeleton({ className = 'h-32' }) {
  return <div className={cn('skeleton-shimmer rounded-xl', className)} />;
}

export function CardError({ message }) {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-2 rounded-xl bg-destructive/5 py-6 text-center">
      <TriangleAlert className="size-5 text-destructive" />
      <p className="max-w-xs text-xs text-muted-foreground">{message || 'Something went wrong loading this data.'}</p>
    </div>
  );
}

export function CardEmpty({ message }) {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-2 rounded-xl bg-muted/50 py-6 text-center">
      <Inbox className="size-5 text-muted-foreground" />
      <p className="max-w-xs text-xs text-muted-foreground">{message || 'Nothing here yet.'}</p>
    </div>
  );
}
