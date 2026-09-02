import { CheckCircle2Icon, CircleAlertIcon, CircleHelpIcon, XCircleIcon } from 'lucide-react';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import type { QcReport, QcStatus } from '@/lib/database.types';
import { cn } from '@/lib/utils';

const STATUS_ICON: Record<QcStatus, typeof CheckCircle2Icon> = {
  pass: CheckCircle2Icon,
  warn: CircleAlertIcon,
  fail: XCircleIcon,
};

const STATUS_COLOR: Record<QcStatus, string> = {
  pass: 'text-emerald-600 dark:text-emerald-400',
  warn: 'text-amber-600 dark:text-amber-400',
  fail: 'text-destructive',
};

/**
 * The mechanical checks that run before a person is asked to look at anything —
 * file integrity, audio levels, caption presence and slideshow risk. The point
 * of showing them is so a reviewer spends their attention on whether the
 * message is right, not on catching a cut that lost its audio.
 */
export function QcReportCard({ report }: { report: QcReport | null }) {
  if (!report) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Quality check</CardTitle>
          <CardDescription>This cut has not been through the quality check yet.</CardDescription>
        </CardHeader>
      </Card>
    );
  }

  const failures = report.checks.filter((check) => check.status === 'fail');

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2 text-base">
          Quality check
          <span className={cn('text-sm font-normal', report.passed ? STATUS_COLOR.pass : STATUS_COLOR.fail)}>
            {report.passed ? 'passed' : 'failed'}
          </span>
        </CardTitle>
        <CardDescription>
          Automated checks on the file itself. They say nothing about whether the claims are true or on-brand — that
          judgement is yours.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {failures.length > 0 && (
          <Alert variant="destructive">
            <CircleAlertIcon className="size-4" />
            <AlertTitle>{failures.length === 1 ? 'One check failed' : `${failures.length} checks failed`}</AlertTitle>
            <AlertDescription>
              Approving anyway publishes a cut the pipeline believes is broken. Prefer rejecting with a note so it can
              be re-rendered.
            </AlertDescription>
          </Alert>
        )}

        <ul className="divide-y">
          {report.checks.map((check) => {
            const Icon = STATUS_ICON[check.status] ?? CircleHelpIcon;
            return (
              <li key={check.key} className="flex items-start gap-3 py-2.5 first:pt-0 last:pb-0">
                <Icon className={cn('mt-0.5 size-4 shrink-0', STATUS_COLOR[check.status])} aria-hidden />
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium">{check.label}</p>
                  {check.detail && <p className="text-sm text-muted-foreground">{check.detail}</p>}
                </div>
                <span className="sr-only">{check.status}</span>
              </li>
            );
          })}
        </ul>

        {report.slideshow_risk !== undefined && (
          <div className="rounded-md bg-muted/50 p-3 text-sm">
            <div className="flex items-center justify-between gap-4">
              <span className="text-muted-foreground">Slideshow risk</span>
              <span className="font-mono tabular-nums">{report.slideshow_risk.toFixed(2)}</span>
            </div>
            <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-border">
              <div
                className={cn(
                  'h-full rounded-full transition-all',
                  report.slideshow_risk >= 0.6
                    ? 'bg-destructive'
                    : report.slideshow_risk >= 0.3
                      ? 'bg-amber-500'
                      : 'bg-emerald-500',
                )}
                style={{ width: `${Math.min(100, Math.max(0, report.slideshow_risk * 100))}%` }}
              />
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
