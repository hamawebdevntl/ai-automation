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
    <div className="flex flex-col gap-4 border-b pb-4 sm:flex-row sm:flex-wrap sm:items-start sm:justify-between">
      <div className="min-w-0 space-y-1">
        <div className="flex items-center gap-2">
          <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
          {count !== undefined && count > 0 && <Badge variant="secondary">{count}</Badge>}
        </div>
        <p className="max-w-2xl text-sm text-muted-foreground">{description}</p>
      </div>
      {/* Full width on a phone, where a lone right-aligned button reads as an
          afterthought and is an awkward reach. Its natural width from `sm`. */}
      {actions && <div className="w-full sm:w-auto sm:shrink-0">{actions}</div>}
    </div>
  );
}
