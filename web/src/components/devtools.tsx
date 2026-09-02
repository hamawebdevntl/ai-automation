import { lazy, Suspense } from 'react';

/**
 * Router and Query devtools, loaded only in development so they never reach a
 * production bundle.
 */
const RouterDevtools = import.meta.env.DEV
  ? lazy(() => import('@tanstack/react-router-devtools').then((module) => ({ default: module.TanStackRouterDevtools })))
  : null;

const QueryDevtools = import.meta.env.DEV
  ? lazy(() => import('@tanstack/react-query-devtools').then((module) => ({ default: module.ReactQueryDevtools })))
  : null;

export function Devtools() {
  if (!RouterDevtools || !QueryDevtools) return null;
  return (
    <Suspense fallback={null}>
      <RouterDevtools position="bottom-right" />
      <QueryDevtools buttonPosition="bottom-left" />
    </Suspense>
  );
}
