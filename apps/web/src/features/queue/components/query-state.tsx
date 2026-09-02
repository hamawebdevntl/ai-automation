import { CircleAlertIcon } from 'lucide-react';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Empty, EmptyDescription, EmptyHeader, EmptyTitle } from '@/components/ui/empty';
import { Skeleton } from '@/components/ui/skeleton';

export function ListSkeleton({ rows = 3 }: { rows?: number }) {
  return (
    <div className="space-y-3">
      {Array.from({ length: rows }, (_, index) => index).map((index) => (
        <Skeleton key={index} className="h-28 w-full rounded-lg" />
      ))}
    </div>
  );
}

/**
 * Row-level security refusals are the one error worth explaining differently:
 * nothing is broken, the row simply is not yours to see.
 */
export function QueryError({ error }: { error: unknown }) {
  const code = (error as { code?: string } | null)?.code;
  const message = error instanceof Error ? error.message : 'Something went wrong loading this.';
  const denied = code === '42501' || code === 'PGRST301';

  return (
    <Alert variant="destructive">
      <CircleAlertIcon className="size-4" />
      <AlertTitle>{denied ? 'Not permitted' : 'Could not load'}</AlertTitle>
      <AlertDescription>
        {denied ? 'Your account does not have access to this. Ask an owner to check your role.' : message}
      </AlertDescription>
    </Alert>
  );
}

export function EmptyQueue({ title, description }: { title: string; description: string }) {
  return (
    <Empty className="rounded-lg border border-dashed">
      <EmptyHeader>
        <EmptyTitle>{title}</EmptyTitle>
        <EmptyDescription>{description}</EmptyDescription>
      </EmptyHeader>
    </Empty>
  );
}
