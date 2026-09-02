import { createFileRoute, Outlet, redirect } from '@tanstack/react-router';
import LayoutOne from '@/components/app-layouts/layout-one/layout-one';

export const Route = createFileRoute('/_app')({
  beforeLoad: ({ context, location }) => {
    if (!context.auth.isAuthenticated) {
      throw redirect({ to: '/login', search: { redirect: location.href } });
    }
  },
  component: AppLayout,
});

function AppLayout() {
  return (
    <LayoutOne>
      <Outlet />
    </LayoutOne>
  );
}
