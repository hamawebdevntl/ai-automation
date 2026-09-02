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
import { isAuthEnabled, isGoogleAuthEnabled, isRegisterEmailAuthEnabled } from '@/lib/feature-flags';

const registerSchema = z
  .object({
    email: z.email({ error: 'Enter a valid email address' }),
    password: z.string().min(8, { error: 'Use at least 8 characters' }),
    confirmPassword: z.string(),
  })
  .refine((value) => value.password === value.confirmPassword, {
    error: 'Passwords do not match',
    path: ['confirmPassword'],
  });

export const Route = createFileRoute('/register')({
  beforeLoad: ({ context }) => {
    if (context.auth.isAuthenticated) throw redirect({ to: '/queue' });
  },
  component: RegisterPage,
});

function RegisterPage() {
  const navigate = useNavigate();
  const { signUpWithPassword } = Route.useRouteContext().auth;
  const [formError, setFormError] = useState<string | null>(null);
  const [confirmationSent, setConfirmationSent] = useState(false);

  const form = useForm({
    defaultValues: { email: '', password: '', confirmPassword: '' },
    validators: { onSubmit: registerSchema },
    onSubmit: async ({ value }) => {
      setFormError(null);
      try {
        const { needsEmailConfirmation } = await signUpWithPassword(value.email, value.password);
        if (needsEmailConfirmation) {
          setConfirmationSent(true);
          return;
        }
        await navigate({ to: '/queue' });
      } catch (error) {
        setFormError(error instanceof Error ? error.message : 'Could not create the account');
      }
    },
  });

  if (!isAuthEnabled() || !isRegisterEmailAuthEnabled()) {
    return (
      <AuthShell title="Registration is closed" description="New accounts are not being created for this deployment.">
        <p className="text-sm text-muted-foreground">
          Ask an owner to invite you from the Supabase dashboard, then{' '}
          <Link to="/login" className="underline underline-offset-4">
            sign in
          </Link>
          .
        </p>
      </AuthShell>
    );
  }

  if (confirmationSent) {
    return (
      <AuthShell title="Check your email" description="Confirm the address to finish creating the account.">
        <p className="text-sm text-muted-foreground">
          New accounts start as <strong>viewers</strong> — able to read the queue, but not to pass either gate. An owner
          promotes them.
        </p>
        <Button asChild variant="outline" className="w-full">
          <Link to="/login">Back to sign in</Link>
        </Button>
      </AuthShell>
    );
  }

  return (
    <AuthShell
      title="Create an account"
      description="New accounts start read-only until an owner promotes them."
      footer={
        <>
          Already have one?{' '}
          <Link to="/login" className="underline underline-offset-4 hover:text-foreground">
            Sign in
          </Link>
        </>
      }
    >
      {formError && (
        <Alert variant="destructive">
          <AlertDescription>{formError}</AlertDescription>
        </Alert>
      )}

      <Form onSubmit={form.handleSubmit} className="space-y-4">
        <form.Field name="email">
          {(field) => <TextField field={field} label="Email" type="email" autoComplete="email" />}
        </form.Field>

        <form.Field name="password">
          {(field) => (
            <TextField
              field={field}
              label="Password"
              type="password"
              autoComplete="new-password"
              description="At least 8 characters."
            />
          )}
        </form.Field>

        <form.Field name="confirmPassword">
          {(field) => <TextField field={field} label="Confirm password" type="password" autoComplete="new-password" />}
        </form.Field>

        <form.Subscribe selector={(state) => state.isSubmitting}>
          {(isSubmitting) => (
            <Button type="submit" className="w-full" disabled={isSubmitting}>
              {isSubmitting && <Spinner className="size-4" />}
              Create account
            </Button>
          )}
        </form.Subscribe>
      </Form>

      {isGoogleAuthEnabled() && (
        <>
          <div className="relative">
            <Separator />
            <span className="absolute inset-0 -top-2 mx-auto w-fit bg-card px-2 text-xs uppercase tracking-wide text-muted-foreground">
              or
            </span>
          </div>
          <GoogleButton label="Sign up with Google" />
        </>
      )}
    </AuthShell>
  );
}
