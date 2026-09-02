import { useForm } from '@tanstack/react-form';
import { createFileRoute, Link } from '@tanstack/react-router';
import { useState } from 'react';
import { z } from 'zod';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Form } from '@/components/ui/form';
import { Spinner } from '@/components/ui/spinner';
import { AuthShell } from '@/features/auth/components/auth-card';
import { TextField } from '@/features/auth/components/field';
import { isAuthEnabled, isForgotPasswordEnabled } from '@/lib/feature-flags';

const schema = z.object({ email: z.email({ error: 'Enter a valid email address' }) });

export const Route = createFileRoute('/forgot-password')({
  component: ForgotPasswordPage,
});

function ForgotPasswordPage() {
  const { sendPasswordReset } = Route.useRouteContext().auth;
  const [formError, setFormError] = useState<string | null>(null);
  const [sent, setSent] = useState(false);

  const form = useForm({
    defaultValues: { email: '' },
    validators: { onSubmit: schema },
    onSubmit: async ({ value }) => {
      setFormError(null);
      try {
        await sendPasswordReset(value.email);
        setSent(true);
      } catch (error) {
        setFormError(error instanceof Error ? error.message : 'Could not send the reset email');
      }
    },
  });

  if (!isAuthEnabled() || !isForgotPasswordEnabled()) {
    return (
      <AuthShell title="Unavailable" description="Password reset is disabled for this deployment.">
        <Button asChild variant="outline" className="w-full">
          <Link to="/login">Back to sign in</Link>
        </Button>
      </AuthShell>
    );
  }

  if (sent) {
    return (
      <AuthShell title="Check your email" description="If that address has an account, a reset link is on its way.">
        <Button asChild variant="outline" className="w-full">
          <Link to="/login">Back to sign in</Link>
        </Button>
      </AuthShell>
    );
  }

  return (
    <AuthShell
      title="Reset your password"
      description="We'll email you a link."
      footer={
        <Link to="/login" className="underline underline-offset-4 hover:text-foreground">
          Back to sign in
        </Link>
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

        <form.Subscribe selector={(state) => state.isSubmitting}>
          {(isSubmitting) => (
            <Button type="submit" className="w-full" disabled={isSubmitting}>
              {isSubmitting && <Spinner className="size-4" />}
              Send reset link
            </Button>
          )}
        </form.Subscribe>
      </Form>
    </AuthShell>
  );
}
