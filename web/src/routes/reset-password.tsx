import { useForm } from '@tanstack/react-form';
import { createFileRoute, Link, useNavigate } from '@tanstack/react-router';
import { useState } from 'react';
import { z } from 'zod';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Form } from '@/components/ui/form';
import { Spinner } from '@/components/ui/spinner';
import { AuthShell } from '@/features/auth/components/auth-card';
import { TextField } from '@/features/auth/components/field';
import { isAuthEnabled, isResetPasswordEnabled } from '@/lib/feature-flags';

const schema = z
  .object({
    password: z.string().min(8, { error: 'Use at least 8 characters' }),
    confirmPassword: z.string(),
  })
  .refine((value) => value.password === value.confirmPassword, {
    error: 'Passwords do not match',
    path: ['confirmPassword'],
  });

export const Route = createFileRoute('/reset-password')({
  component: ResetPasswordPage,
});

function ResetPasswordPage() {
  const navigate = useNavigate();
  const { updatePassword, isAuthenticated } = Route.useRouteContext().auth;
  const [formError, setFormError] = useState<string | null>(null);

  const form = useForm({
    defaultValues: { password: '', confirmPassword: '' },
    validators: { onSubmit: schema },
    onSubmit: async ({ value }) => {
      setFormError(null);
      try {
        await updatePassword(value.password);
        await navigate({ to: '/queue' });
      } catch (error) {
        setFormError(error instanceof Error ? error.message : 'Could not update the password');
      }
    },
  });

  if (!isAuthEnabled() || !isResetPasswordEnabled()) {
    return (
      <AuthShell title="Unavailable" description="Password reset is disabled for this deployment.">
        <Button asChild variant="outline" className="w-full">
          <Link to="/login">Back to sign in</Link>
        </Button>
      </AuthShell>
    );
  }

  // Following the emailed link signs the browser in with a recovery session,
  // which is what makes `updateUser({ password })` permissible. Without it
  // there is nothing to update.
  if (!isAuthenticated) {
    return (
      <AuthShell title="Link expired" description="Open the most recent reset email, or request a new link.">
        <Button asChild variant="outline" className="w-full">
          <Link to="/forgot-password">Request a new link</Link>
        </Button>
      </AuthShell>
    );
  }

  return (
    <AuthShell title="Choose a new password" description="You are signed in from the reset link.">
      {formError && (
        <Alert variant="destructive">
          <AlertDescription>{formError}</AlertDescription>
        </Alert>
      )}

      <Form onSubmit={form.handleSubmit} className="space-y-4">
        <form.Field name="password">
          {(field) => <TextField field={field} label="New password" type="password" autoComplete="new-password" />}
        </form.Field>

        <form.Field name="confirmPassword">
          {(field) => <TextField field={field} label="Confirm password" type="password" autoComplete="new-password" />}
        </form.Field>

        <form.Subscribe selector={(state) => state.isSubmitting}>
          {(isSubmitting) => (
            <Button type="submit" className="w-full" disabled={isSubmitting}>
              {isSubmitting && <Spinner className="size-4" />}
              Update password
            </Button>
          )}
        </form.Subscribe>
      </Form>
    </AuthShell>
  );
}
