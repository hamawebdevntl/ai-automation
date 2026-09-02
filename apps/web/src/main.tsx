import { QueryClientProvider } from '@tanstack/react-query';
import { createRouter, RouterProvider } from '@tanstack/react-router';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { ThemeProvider } from '@/components/theme-provider';
import { AuthProvider, useAuth } from '@/features/auth/auth-context';
import { envIssues } from '@/lib/env';
import { createQueryClient } from '@/lib/query-client';
import { routeTree } from './routeTree.gen';
import '@/styles/globals.css';

const queryClient = createQueryClient();

const router = createRouter({
  routeTree,
  // `auth` cannot exist before React does, so it is injected by <App /> below.
  // The router only needs the context's *shape* at creation time.
  // biome-ignore lint/style/noNonNullAssertion: placeholder replaced by RouterProvider's context prop
  context: { queryClient, auth: undefined! },
  defaultPreload: 'intent',
  defaultPreloadStaleTime: 0,
  scrollRestoration: true,
});

declare module '@tanstack/react-router' {
  interface Register {
    router: typeof router;
  }
}

function Splash() {
  return <div className="flex min-h-svh items-center justify-center text-sm text-muted-foreground">Loading…</div>;
}

/**
 * Shown instead of the app when the build has no usable Supabase configuration.
 * A blank screen and a console error is a poor way to learn that a `VITE_`
 * variable was missing at build time.
 */
function ConfigurationError() {
  return (
    <div className="mx-auto flex min-h-svh max-w-xl flex-col justify-center gap-4 p-6">
      <h1 className="text-xl font-semibold tracking-tight">This build is not configured</h1>
      <p className="text-sm text-muted-foreground">
        The app could not read its Supabase settings. These are baked in when the bundle is built, so this needs fixing
        at build time — not in the browser.
      </p>
      <ul className="list-inside list-disc space-y-1 rounded-md border bg-muted/40 p-4 font-mono text-xs">
        {envIssues.map((issue) => (
          <li key={issue}>{issue}</li>
        ))}
      </ul>
      <p className="text-sm text-muted-foreground">
        Copy <code className="rounded bg-muted px-1">.env.example</code> to{' '}
        <code className="rounded bg-muted px-1">.env</code> and rebuild.
      </p>
    </div>
  );
}

function App() {
  const auth = useAuth();
  return <RouterProvider router={router} context={{ auth }} />;
}

const rootElement = document.getElementById('root');
if (!rootElement) throw new Error('No #root element in index.html');

createRoot(rootElement).render(
  <StrictMode>
    <ThemeProvider attribute="class" defaultTheme="system" enableSystem disableTransitionOnChange>
      {envIssues.length > 0 ? (
        <ConfigurationError />
      ) : (
        <QueryClientProvider client={queryClient}>
          <AuthProvider fallback={<Splash />}>
            <App />
          </AuthProvider>
        </QueryClientProvider>
      )}
    </ThemeProvider>
  </StrictMode>,
);
