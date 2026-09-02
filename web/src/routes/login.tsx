import { useForm } from '@tanstack/react-form';
import { createFileRoute, Link, redirect, useNavigate } from '@tanstack/react-router';
import { useState } from 'react';
import { z } from 'zod';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Form } from '@/components/ui/form';
import { Separator } from '@/components/ui/separator';
import { Spinner } from '@/components/ui/spinner';
import { AuthShell } from '@/features/auth/components/auth-card';
import { TextField } from '@/features/auth/components/field';
import { GoogleButton } from '@/features/auth/components/google-button';
import {
  isAuthEnabled,
  isForgotPasswordEnabled,
  isGoogleAuthEnabled,
  isLoginEmailAuthEnabled,
  isRegisterEmailAuthEnabled,
} from '@/lib/feature-flags';
import { toRoutePath } from '@/lib/routing';

const searchSchema = z.object({
  /** Where to land after signing in. Kept relative so it cannot bounce off-site. */
  redirect: z
    .string()
    .optional()
    .refine((value) => value === undefined || value.startsWith('/'), { error: 'redirect must be a relative path' }),
});

const loginSchema = z.object({
  email: z.email({ error: 'Enter a valid email address' }),
  password: z.string().min(1, { error: 'Enter your password' }),
});

export const Route = createFileRoute('/login')({
  validateSearch: searchSchema,
  beforeLoad: ({ context, search }) => {
    if (context.auth.isAuthenticated) {
      throw redirect({ to: toRoutePath(search.redirect, '/queue') });
    }
  },
  component: LoginPage,
});

function LoginPage() {
  const { redirect: redirectTo } = Route.useSearch();
  const navigate = useNavigate();
  const { signInWithPassword } = Route.useRouteContext().auth;
  const [formError, setFormError] = useState<string | null>(null);

  const form = useForm({
    defaultValues: { email: '', password: '' },
    validators: { onSubmit: loginSchema },
    onSubmit: async ({ value }) => {
      setFormError(null);
      try {
        await signInWithPassword(value.email, value.password);
        await navigate({ to: toRoutePath(redirectTo, '/queue') });
      } catch (error) {
        setFormError(error instanceof Error ? error.message : 'Could not sign in');
      }
    },
  });

  if (!isAuthEnabled()) {
    return (
      <AuthShell title="Sign-in is closed" description="Authentication is disabled for this deployment.">
        <p className="text-sm text-muted-foreground">
          Set <code className="rounded bg-muted px-1 py-0.5">VITE_AUTH_ENABLED=1</code> to turn it back on.
        </p>
      </AuthShell>
    );
  }

  const emailEnabled = isLoginEmailAuthEnabled();
  const googleEnabled = isGoogleAuthEnabled();

  return (
    <AuthShell
      title="Sign in"
      description="Approvals for the reels pipeline."
      footer={
        isRegisterEmailAuthEnabled() ? (
          <>
            No account?{' '}
            <Link to="/register" className="underline underline-offset-4 hover:text-foreground">
              Create one
            </Link>
          </>
        ) : null
      }
    >
      {formError && (
        <Alert variant="destructive">
          <AlertDescription>{formError}</AlertDescription>
        </Alert>
      )}

      {emailEnabled && (
        <Form onSubmit={form.handleSubmit} className="space-y-4">
          <form.Field name="email">
            {(field) => <TextField field={field} label="Email" type="email" autoComplete="email" />}
          </form.Field>

          <form.Field name="password">
            {(field) => <TextField field={field} label="Password" type="password" autoComplete="current-password" />}
          </form.Field>

          <form.Subscribe selector={(state) => state.isSubmitting}>
            {(isSubmitting) => (
              <Button type="submit" className="w-full" disabled={isSubmitting}>
                {isSubmitting && <Spinner className="size-4" />}
                Sign in
              </Button>
            )}
          </form.Subscribe>

          {isForgotPasswordEnabled() && (
            <div className="text-center text-sm">
              <Link
                to="/forgot-password"
                className="text-muted-foreground underline underline-offset-4 hover:text-foreground"
              >
                Forgot your password?
              </Link>
            </div>
          )}
        </Form>
      )}

      {emailEnabled && googleEnabled && (
        <div className="relative">
          <Separator />
          <span className="absolute inset-0 -top-2 mx-auto w-fit bg-card px-2 text-xs uppercase tracking-wide text-muted-foreground">
            or
          </span>
        </div>
      )}

      {googleEnabled && <GoogleButton redirectTo={redirectTo} />}

      {!emailEnabled && !googleEnabled && (
        <p className="text-sm text-muted-foreground">
          Every sign-in method is disabled. Enable one of the <code>VITE_*_ENABLED</code> flags.
        </p>
      )}
    </AuthShell>
  );
}
