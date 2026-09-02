import { Badge } from '@/components/ui/badge';

export function PageHeader({
  title,
  description,
  count,
  actions,
}: {
  title: string;
  description: string;
  count?: number;
  actions?: React.ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-4 border-b pb-4">
      <div className="space-y-1">
        <div className="flex items-center gap-2">
          <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
          {count !== undefined && count > 0 && <Badge variant="secondary">{count}</Badge>}
        </div>
        <p className="max-w-2xl text-sm text-muted-foreground">{description}</p>
      </div>
      {actions}
    </div>
  );
}
