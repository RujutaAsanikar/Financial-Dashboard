import { cn } from '../lib/utils';

export default function Card({ title, description, action, children, className = '', bodyClassName = '' }) {
  return (
    <div
      className={cn(
        'flex min-h-[160px] flex-col gap-3 rounded-2xl border border-border bg-card p-5 text-card-foreground shadow-sm transition-shadow hover:shadow-md sm:p-6',
        className
      )}
    >
      {(title || action) && (
        <div className="flex items-start justify-between gap-3">
          {title && (
            <div>
              <h3 className="text-sm font-semibold tracking-tight text-foreground">{title}</h3>
              {description && <p className="mt-0.5 text-xs text-muted-foreground">{description}</p>}
            </div>
          )}
          {action}
        </div>
      )}
      <div className={cn('flex flex-1 flex-col', bodyClassName)}>{children}</div>
    </div>
  );
}
