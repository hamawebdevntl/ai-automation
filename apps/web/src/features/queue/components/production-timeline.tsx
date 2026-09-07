import {
  CheckCircle2Icon,
  ChevronDownIcon,
  CircleDashedIcon,
  CircleDotIcon,
  CircleSlashIcon,
  ClockIcon,
  UserIcon,
  XCircleIcon,
} from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible';
import { Progress } from '@/components/ui/progress';
import { Spinner } from '@/components/ui/spinner';
import type { ProductionEventRow, ProductionRow } from '@/lib/database.types';
import { formatRelative } from '@/lib/format';
import { cn } from '@/lib/utils';
import { buildTimeline, currentGraphStep, renderProgress, type StepState, type TimelineStep } from '../pipeline-steps';

/**
 * Every step from approval to final output, and what happened at each.
 *
 * Two rules this follows deliberately.
 *
 * **Every step is drawn, including the ones not reached.** The same choice
 * `RunBreakdown` makes for trend stages. A step that only appears once it is
 * running is a step nobody can ask a question about beforehand.
 *
 * **The technical record is one click away, not hidden and not in the way.**
 * Retry causes, provider ids, quality-check numbers and raw errors all live
 * under the step they belong to. The default reading should be a sentence;
 * the detail should be there when the sentence is not enough.
 */

const STATE_ICON: Record<StepState, typeof CheckCircle2Icon> = {
  pending: CircleDashedIcon,
  active: CircleDotIcon,
  waiting: ClockIcon,
  done: CheckCircle2Icon,
  failed: XCircleIcon,
  skipped: CircleSlashIcon,
};

const STATE_COLOR: Record<StepState, string> = {
  pending: 'text-muted-foreground/40',
  active: 'text-blue-600 dark:text-blue-400',
  waiting: 'text-amber-600 dark:text-amber-400',
  done: 'text-emerald-600 dark:text-emerald-400',
  failed: 'text-destructive',
  skipped: 'text-muted-foreground',
};

const STATE_LABEL: Record<StepState, string> = {
  pending: 'Not yet',
  active: 'Running',
  waiting: 'Waiting for you',
  done: 'Done',
  failed: 'Stopped',
  skipped: 'Skipped',
};

/** How an individual log entry reads. */
const OUTCOME_STYLE: Record<string, { label: string; className: string }> = {
  started: { label: 'started', className: 'text-muted-foreground' },
  progress: { label: 'progress', className: 'text-muted-foreground' },
  succeeded: { label: 'done', className: 'text-emerald-600 dark:text-emerald-400' },
  retrying: { label: 'retrying', className: 'text-amber-600 dark:text-amber-400' },
  infra_retry: { label: 'connection', className: 'text-amber-600 dark:text-amber-400' },
  waiting: { label: 'waiting', className: 'text-muted-foreground' },
  failed: { label: 'failed', className: 'text-destructive' },
  parked: { label: 'stopped', className: 'text-destructive' },
  terminal: { label: 'finished', className: 'text-muted-foreground' },
  control: { label: 'you', className: 'text-blue-600 dark:text-blue-400' },
};

export interface ProductionTimelineProps {
  production: ProductionRow;
  events: ProductionEventRow[];
  /** Still loading the log. The steps are drawn from the row and need no wait. */
  isLoadingEvents?: boolean;
}

export function ProductionTimeline({ production, events, isLoadingEvents }: ProductionTimelineProps) {
  const steps = buildTimeline(production);
  const eventsByStep = groupEvents(events, steps);

  return (
    <ol className="relative space-y-1">
      {steps.map((step, index) => (
        <TimelineRow
          key={step.key}
          step={step}
          production={production}
          events={eventsByStep.get(step.key) ?? []}
          isLast={index === steps.length - 1}
          isLoadingEvents={isLoadingEvents}
        />
      ))}
    </ol>
  );
}

/**
 * Put each log entry under the display step it belongs to.
 *
 * A `control` event names no driver step, so it is filed against wherever the
 * production is now — which is where an owner reading the page will look for
 * the thing they just did.
 */
function groupEvents(events: ProductionEventRow[], steps: TimelineStep[]): Map<string, ProductionEventRow[]> {
  const stepForGraphStep = new Map<string, string>();
  for (const step of steps) {
    for (const graphStep of step.graphSteps) stepForGraphStep.set(graphStep, step.key);
  }

  const grouped = new Map<string, ProductionEventRow[]>();
  const activeKey = steps.find((step) => step.state === 'active' || step.state === 'waiting')?.key;

  for (const event of events) {
    const key = event.step === 'control' ? (activeKey ?? 'done') : (stepForGraphStep.get(event.step) ?? 'done');
    const bucket = grouped.get(key);
    if (bucket) bucket.push(event);
    else grouped.set(key, [event]);
  }
  return grouped;
}

function TimelineRow({
  step,
  production,
  events,
  isLast,
  isLoadingEvents,
}: {
  step: TimelineStep;
  production: ProductionRow;
  events: ProductionEventRow[];
  isLast: boolean;
  isLoadingEvents?: boolean;
}) {
  const Icon = STATE_ICON[step.state];
  const progress = step.key === 'render' && step.state === 'active' ? renderProgress(production) : null;
  const stage = step.state === 'active' ? production.stage?.trim() : null;
  const hasDetail = events.length > 0;

  return (
    <li className="relative flex gap-3">
      {/* The spine. Stops at the last step so the timeline has an end. */}
      {!isLast && <span aria-hidden className="absolute left-[11px] top-7 bottom-0 w-px bg-border" />}

      <span className={cn('relative z-10 mt-1 shrink-0 bg-background', STATE_COLOR[step.state])}>
        {step.state === 'active' ? <Spinner className="size-[22px]" /> : <Icon className="size-[22px]" aria-hidden />}
      </span>

      <div className="min-w-0 flex-1 pb-4">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span className={cn('text-sm font-medium', step.state === 'pending' && 'text-muted-foreground')}>
            {step.label}
          </span>
          <Badge variant="outline" className={cn('font-normal', STATE_COLOR[step.state])}>
            {STATE_LABEL[step.state]}
          </Badge>
          {step.isGate && (
            <Badge variant="secondary" className="font-normal">
              needs a person
            </Badge>
          )}
        </div>

        <p className="mt-0.5 text-sm text-muted-foreground">{step.description}</p>

        {/* `stage` is free text the activities write. It is a live sub-label
            here and never the thing the timeline's position is read from. */}
        {stage && <p className="mt-1 text-sm">{stage}</p>}

        {progress !== null && (
          <div className="mt-2 flex items-center gap-2">
            <Progress value={progress} className="h-1.5 max-w-xs" />
            <span className="text-xs tabular-nums text-muted-foreground">{Math.round(progress)}%</span>
          </div>
        )}

        {/* Only at the step that actually failed. The outcome row is 'failed'
            too when a production stopped, and printing the same error twice
            reads as two problems. */}
        {step.state === 'failed' && step.key !== 'done' && production.error && (
          <p className="mt-2 rounded-md bg-destructive/10 px-2.5 py-1.5 text-sm text-destructive break-words">
            {production.error}
          </p>
        )}

        {hasDetail ? (
          <StepLog events={events} />
        ) : (
          isLoadingEvents &&
          step.state !== 'pending' && <p className="mt-2 text-xs text-muted-foreground">Loading the record…</p>
        )}
      </div>
    </li>
  );
}

function StepLog({ events }: { events: ProductionEventRow[] }) {
  return (
    <Collapsible className="mt-2">
      <CollapsibleTrigger className="group flex items-center gap-1 text-xs text-muted-foreground underline-offset-4 hover:underline">
        <ChevronDownIcon className="size-3.5 transition-transform group-data-[state=open]:rotate-180" />
        {events.length === 1 ? '1 entry' : `${events.length} entries`}
      </CollapsibleTrigger>
      <CollapsibleContent>
        <ul className="mt-2 space-y-2 border-l pl-3">
          {events.map((event) => (
            <LogEntry key={event.id} event={event} />
          ))}
        </ul>
      </CollapsibleContent>
    </Collapsible>
  );
}

function LogEntry({ event }: { event: ProductionEventRow }) {
  const style = OUTCOME_STYLE[event.outcome] ?? { label: event.outcome, className: 'text-muted-foreground' };
  const payload = readPayload(event.payload);

  return (
    <li className="space-y-1 text-xs">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
        <span className={cn('font-medium', style.className)}>{style.label}</span>
        {event.outcome === 'control' && <UserIcon className="size-3 text-muted-foreground" aria-hidden />}
        {event.attempt !== null && <span className="text-muted-foreground">attempt {event.attempt}</span>}
        <span className="text-muted-foreground">{formatRelative(event.created_at)}</span>
      </div>

      {event.detail && <p className="text-muted-foreground break-words">{event.detail}</p>}

      {event.error && (
        <p className="rounded bg-destructive/10 px-2 py-1 font-mono text-[11px] text-destructive break-all">
          {event.error}
        </p>
      )}

      {payload.length > 0 && (
        <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5 font-mono text-[11px] text-muted-foreground">
          {payload.map(([key, value]) => (
            <div key={key} className="contents">
              <dt className="truncate">{key}</dt>
              <dd className="truncate break-all">{value}</dd>
            </div>
          ))}
        </dl>
      )}
    </li>
  );
}

/**
 * The technical half of an event, flattened for display.
 *
 * Values are stringified rather than pretty-printed: this is a reference for
 * somebody diagnosing a stopped production — a fal request id, a HeyGen video
 * id, a storage key — not a data structure to browse.
 */
function readPayload(payload: unknown): [string, string][] {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return [];
  return Object.entries(payload as Record<string, unknown>)
    .filter(([, value]) => value !== null && value !== undefined && value !== '')
    .map(([key, value]) => [key, typeof value === 'object' ? JSON.stringify(value) : String(value)]);
}

/** The one-line version, for a list row. */
export function ProductionStepSummary({ production }: { production: ProductionRow }) {
  const steps = buildTimeline(production);
  const done = steps.filter((step) => step.state === 'done').length;
  const current = steps.find((step) => step.state === 'active' || step.state === 'waiting' || step.state === 'failed');

  return (
    <span className="text-xs text-muted-foreground">
      {current ? `${current.label} · ` : ''}
      {done} of {steps.length} steps · {currentGraphStep(production)}
    </span>
  );
}
