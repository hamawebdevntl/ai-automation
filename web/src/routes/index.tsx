import { createFileRoute, redirect } from '@tanstack/react-router';

/**
 * There is no marketing surface here — this app is internal. The root just
 * decides which side of the login boundary you are on.
 */
export const Route = createFileRoute('/')({
  beforeLoad: ({ context }) => {
    throw redirect({ to: context.auth.isAuthenticated ? '/queue' : '/login' });
  },
});
