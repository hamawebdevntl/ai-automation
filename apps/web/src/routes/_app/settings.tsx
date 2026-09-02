import { createFileRoute } from '@tanstack/react-router';
import { toast } from 'sonner';
import { ModeToggle } from '@/components/mode-toggle';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Separator } from '@/components/ui/separator';
import { useAuth } from '@/features/auth/auth-context';
import { useOwner } from '@/features/auth/use-owner';
import { PageHeader } from '@/features/queue/components/page-header';

export const Route = createFileRoute('/_app/settings')({
  component: SettingsPage,
});

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 py-2.5">
      <span className="text-sm text-muted-foreground">{label}</span>
      <span className="text-sm">{children}</span>
    </div>
  );
}

function SettingsPage() {
  const { user, signOut } = useAuth();
  const { role, isOwner } = useOwner();

  return (
    <div className="mx-auto w-full max-w-2xl space-y-6">
      <PageHeader title="Settings" description="Your account, and how this app is wired." />

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Account</CardTitle>
        </CardHeader>
        <CardContent className="divide-y">
          <Row label="Email">{user?.email ?? '—'}</Row>
          <Row label="Role">
            <Badge variant={isOwner ? 'default' : 'secondary'}>{role ?? 'unknown'}</Badge>
          </Row>
          <Row label="Theme">
            <ModeToggle />
          </Row>
        </CardContent>
      </Card>

      {!isOwner && (
        <Alert>
          <AlertTitle>You are a viewer</AlertTitle>
          <AlertDescription>
            Viewers read the queue; owners pass the gates. Promotion is a one-line change in Supabase:
            <code className="mt-2 block rounded bg-muted px-2 py-1 font-mono text-xs">
              update public.profiles set role = 'owner' where email = '{user?.email ?? 'you@example.com'}';
            </code>
          </AlertDescription>
        </Alert>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">How this app is wired</CardTitle>
          <CardDescription>Worth knowing before you go looking for a server.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3 text-sm text-muted-foreground">
          <p>
            This is a static single-page app. It talks to Supabase directly from the browser and holds no secrets, so
            what you are allowed to do is decided by row-level security in Postgres — not by anything shipped here.
          </p>
          <p>
            Both gate decisions go through database functions, which is what keeps the decision and its audit row from
            coming apart, and what refuses a viewer trying to approve.
          </p>
          <p>
            The pipeline itself — trend research, production, quality check, publishing — runs on AWS and writes to the
            same tables with a service-role key.
          </p>
        </CardContent>
      </Card>

      <Separator />

      <Button
        variant="outline"
        onClick={async () => {
          try {
            await signOut();
          } catch (error) {
            toast.error(error instanceof Error ? error.message : 'Could not sign out');
          }
        }}
      >
        Sign out
      </Button>
    </div>
  );
}
