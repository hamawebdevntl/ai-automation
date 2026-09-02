import type { QueryClient } from '@tanstack/react-query';
import { createRootRouteWithContext, Link, Outlet } from '@tanstack/react-router';
import { Devtools } from '@/components/devtools';
import { Button } from '@/components/ui/button';
import { Toaster } from '@/components/ui/sonner';
import type { AuthContextValue } from '@/features/auth/auth-context';

export interface RouterContext {
  queryClient: QueryClient;
  auth: AuthContextValue;
}

export const Route = createRootRouteWithContext<RouterContext>()({
  component: RootLayout,
  notFoundComponent: NotFound,
  errorComponent: RouteError,
});

function RootLayout() {
  return (
    <>
      <Outlet />
      <Toaster />
      <Devtools />
    </>
  );
}

function CentredMessage({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="flex min-h-svh flex-col items-center justify-center gap-4 p-6 text-center">
      <div className="space-y-2">
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        <p className="max-w-prose text-muted-foreground">{description}</p>
      </div>
      {children}
    </div>
  );
}

function NotFound() {
  return (
    <CentredMessage title="Not found" description="That page does not exist in this app.">
      <Button asChild>
        <Link to="/queue">Back to the queue</Link>
      </Button>
    </CentredMessage>
  );
}

function RouteError({ error }: { error: Error }) {
  return (
    <CentredMessage title="Something broke" description={error.message || 'An unexpected error occurred.'}>
      <Button variant="outline" onClick={() => window.location.reload()}>
        Reload
      </Button>
    </CentredMessage>
  );
}
