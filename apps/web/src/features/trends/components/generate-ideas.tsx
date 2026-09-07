import { useQuery } from '@tanstack/react-query';
import { Link } from '@tanstack/react-router';
import { CircleAlertIcon, CircleStopIcon, SparklesIcon } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Spinner } from '@/components/ui/spinner';
import { useOwner } from '@/features/auth/use-owner';
import {
  isAlreadyRunningError,
  isNothingToStopError,
  isUnclaimed,
  latestTrendRunQueryOptions,
  SCOUT_CADENCE_MINUTES,
  TREND_RUN_MINUTES,
  trendSettingsQueryOptions,
  useCancelTrendRun,
  useRequestTrendRun,
} from '@/features/trends/api';
import { RunBreakdown } from '@/features/trends/components/run-breakdown';
import {
  DEFAULT_SEARCH_LENGTH,
  SearchLength,
  type SearchLengthChoice,
} from '@/features/trends/components/search-length';
import { diagnoseRun, estimateSearchLength } from '@/features/trends/controls';
import { isTrendRunInFlight, type TrendRunRow } from '@/lib/database.types';
import { formatMinutes, formatRelative } from '@/lib/format';
import { toError } from '@/lib/supabase-error';

/**
 * Asking for a trend run, and watching one.
 *
 * Split into a button and a banner because they belong in different places --
 * the button sits in the page header beside the count, the banner spans the
 * list -- while both read the same polled row. Two `useQuery` calls against one
 * key is one request; splitting the component is free.
 */

export function GenerateIdeasButton() {
  const { isOwner, isLoading: isRoleLoading } = useOwner();
  const { data: run } = useQuery(latestTrendRunQueryOptions());
  const { data: settings } = useQuery(trendSettingsQueryOptions());
  const request = useRequestTrendRun();

  // Held here rather than in `SearchLength` so the button can send it. Reset to
  // the saved settings on every mount, deliberately: a length is chosen for a
  // reason that belongs to the moment, and a stale choice silently applying to
  // next week's run is the surprise this whole design avoids.
  const [length, setLength] = useState<SearchLengthChoice>(DEFAULT_SEARCH_LENGTH);

  const inFlight = isTrendRunInFlight(run);
  const disabled = !isOwner || inFlight || request.isPending;

  async function onClick() {
    try {
      await request.mutateAsync({
        budgetMinutes: length.budgetMinutes,
        hashtagsPerRun: length.hashtagsPerRun,
      });
      const minutes = settings
        ? estimateSearchLength(settings, {
            hashtagsPerRun: length.hashtagsPerRun,
            budgetMinutes: length.budgetMinutes,
          }).minutes
        : TREND_RUN_MINUTES;
      toast.success('Looking for new ideas', {
        description: `Scouting takes up to ${formatMinutes(minutes)}. You can leave this page.`,
      });
    } catch (error) {
      if (isAlreadyRunningError(error)) {
        toast.info('A run is already going', { description: 'Only one can run at a time.' });
        return;
      }
      // The reason goes in the description rather than replacing the headline:
      // a bare PostgREST sentence is no use without knowing what it refused.
      // `toError` is what makes there be a reason at all -- see its module.
      toast.error('Could not start a trend run', { description: toError(error).message });
    }
  }

  // A disabled button explains nothing on its own. Said in text rather than in
  // a tooltip on purpose: a tooltip cannot be hovered on a phone, which is
  // exactly where an unexplained dead button is most confusing. The in-flight
  // case needs nothing here -- the button says it and the banner says more.
  const reason = !isOwner && !isRoleLoading ? 'Only an owner can start a trend run.' : null;

  return (
    <div className="w-full space-y-2 sm:w-auto">
      {/* Above the button, because it is a decision about the run the button
          is going to start. Hidden while one is in flight -- there is nothing
          to configure about a run that has already begun -- and hidden from a
          viewer, who cannot start one. */}
      {isOwner && !inFlight && <SearchLength value={length} onChange={setLength} disabled={request.isPending} />}
      <Button onClick={onClick} disabled={disabled} className="w-full sm:w-auto">
        {inFlight || request.isPending ? <Spinner className="size-4" /> : <SparklesIcon className="size-4" />}
        {inFlight ? 'Looking for ideas\u2026' : 'Generate more ideas'}
      </Button>
      {reason && <p className="text-xs text-muted-foreground sm:text-right">{reason}</p>}
    </div>
  );
}

/**
 * What the last run did, when that is still worth saying.
 *
 * Silent on a plain success: the ideas themselves are the result, and a green
 * bar above them saying so is clutter. It speaks while a run is going, because
 * an hour of nothing looks identical to a broken button, and when a run failed
 * or found nothing, because both leave the queue looking untouched.
 */
export function TrendRunBanner() {
  const { data: run } = useQuery(latestTrendRunQueryOptions());
  if (!run) return null;

  if (isTrendRunInFlight(run)) return <InFlight run={run} />;
  if (run.status === 'cancelled') return <Stopped run={run} />;
  if (run.status === 'failed') return <Failed run={run} />;
  if (run.status === 'succeeded' && run.inserted === 0) return <FoundNothing run={run} />;
  return null;
}

/**
 * Stop the run that is going.
 *
 * Present in both in-flight states, and it matters most in the one that looks
 * broken. At most one run may be in flight, so a row nothing will ever claim
 * does not hold up one run -- it holds up every future run. Before this, the
 * only thing that could clear it was the dispatcher, which is exactly what is
 * missing whenever a run gets stuck that way.
 *
 * This calls a database function and nothing else, which is the entire point:
 * it works when none of the rest of the pipeline does.
 */
function StopRunButton({ run }: { run: TrendRunRow }) {
  const { isOwner } = useOwner();
  const cancel = useCancelTrendRun();
  if (!isOwner) return null;

  async function onClick() {
    try {
      await cancel.mutateAsync(run.id);
      toast.success('Run stopped', {
        description:
          run.status === 'running'
            ? 'You can start another now. The scout it was using shuts down within a minute or so.'
            : 'You can start another now.',
      });
    } catch (error) {
      if (isNothingToStopError(error)) {
        // Not a failure: the run ended by itself between the page loading and
        // the button being pressed, which is the outcome that was wanted.
        toast.info('That run had already finished', { description: 'Nothing needed stopping.' });
        return;
      }
      toast.error('Could not stop the run', { description: toError(error).message });
    }
  }

  return (
    <Button variant="outline" size="sm" onClick={onClick} disabled={cancel.isPending} className="w-fit">
      <CircleStopIcon className="size-4" />
      {cancel.isPending ? 'Stopping…' : 'Stop this run'}
    </Button>
  );
}

/**
 * A run the owner stopped.
 *
 * Quiet rather than red, and deliberately so. This is the outcome they asked
 * for; dressing it as a failure alongside the genuine ones is how the failure
 * banner stops being read.
 */
function Stopped({ run }: { run: TrendRunRow }) {
  const scouted = run.rejections?.seen ?? 0;
  return (
    <Alert>
      <CircleStopIcon className="size-4" />
      <AlertTitle>You stopped that run</AlertTitle>
      <AlertDescription>
        <span>
          Nothing was added to the queue
          {scouted > 0 ? `, though it had looked at ${scouted.toLocaleString()} videos by then` : ''}. A stopped run
          does not draft ideas from what it had found — stop means stop.
        </span>
        <span>You can start another whenever you like.</span>
      </AlertDescription>
    </Alert>
  );
}

function InFlight({ run }: { run: TrendRunRow }) {
  const started = run.started_at ?? run.requested_at;

  // An unclaimed request is the one in-flight state that can mean something is
  // wrong rather than something is happening. The dispatcher runs every
  // minute, so a row still sitting here after several of them is not a slow
  // start -- it is nothing listening. Saying "usually under a minute" for the
  // twentieth minute running is the sentence that makes a broken deployment
  // look like a working one.
  if (run.status === 'requested' && isUnclaimed(run)) return <NobodyPickedItUp run={run} />;

  return (
    <Alert>
      <Spinner className="size-4" />
      <AlertTitle>Scouting for ideas</AlertTitle>
      <AlertDescription>
        {run.trigger === 'schedule' && 'This is the scheduled run. '}
        {run.status === 'requested'
          ? `Queued for the next scout, which runs about every ${formatMinutes(SCOUT_CADENCE_MINUTES)} — so this may wait a while before it starts.`
          : `Started ${formatRelative(started)}.`}{' '}
        A full pass takes about {formatMinutes(TREND_RUN_MINUTES)}, and new ideas appear here as soon as it finishes.
        You do not need to stay on this page.
        <StopRunButton run={run} />
      </AlertDescription>
    </Alert>
  );
}

function NobodyPickedItUp({ run }: { run: TrendRunRow }) {
  return (
    <Alert variant="destructive">
      <CircleAlertIcon className="size-4" />
      <AlertTitle>Nothing has picked this run up</AlertTitle>
      <AlertDescription>
        <span>
          It was requested {formatRelative(run.requested_at)} and no scout has claimed it. The scout runs about every{' '}
          {formatMinutes(SCOUT_CADENCE_MINUTES)}, so a wait is normal — but this one has now missed a full cycle, which
          usually means the scheduled job is not running.
        </span>
        <span>
          The scout is a thread inside the pipeline worker, so the worker's logs are the place to look (
          <code>docker compose logs worker</code>): a container that is down, a crash loop, or bad credentials all look
          identical from here. Nothing will clear this row on its own — a dispatcher that is not running is not sweeping
          either.
        </span>
        <span>
          Stopping it clears the row without needing the scout. That frees the button — though if nothing is claiming
          runs, the next one will sit here too.
        </span>
        <StopRunButton run={run} />
      </AlertDescription>
    </Alert>
  );
}

function Failed({ run }: { run: TrendRunRow }) {
  return (
    <Alert variant="destructive">
      <CircleAlertIcon className="size-4" />
      <AlertTitle>The last run did not finish</AlertTitle>
      <AlertDescription>
        <span className="break-words">{run.error ?? 'It stopped without saying why.'}</span>
        <span>Nothing was added to the queue. You can start another run.</span>
      </AlertDescription>
    </Alert>
  );
}

/**
 * A run that added nothing, and which of several unrelated reasons it was.
 *
 * This used to say one thing -- it scored N signals and drafted M ideas -- for
 * every empty run there is. That sentence is true of a quiet week, of filters
 * set too tight, and of a scraper being served captchas, and the owner cannot
 * act on any of them from it. Now that the filters are a form, that ambiguity
 * would be actively misleading: the natural reading of an empty queue becomes
 * "the thing I just changed broke it".
 *
 * So the diagnosis comes first, in a sentence, and the funnel is underneath
 * for anyone who wants to see where the videos went.
 */
function FoundNothing({ run }: { run: TrendRunRow }) {
  const diagnosis = diagnoseRun(run);

  // A refused scrape is not the same kind of news as a strict filter. One is
  // something to fix, the other is something to reconsider.
  const variant = diagnosis.kind === 'scraper' ? 'destructive' : 'default';

  return (
    <Alert variant={variant}>
      <CircleAlertIcon className="size-4" />
      <AlertTitle>{diagnosis.headline}</AlertTitle>
      <AlertDescription>
        <span>{diagnosis.detail}</span>

        {run.rejections ? (
          <details className="w-full">
            <summary className="cursor-pointer text-xs underline underline-offset-4">
              Where the {run.rejections.seen.toLocaleString()} videos went
            </summary>
            <div className="pt-3">
              <RunBreakdown run={run} />
            </div>
          </details>
        ) : (
          <span>
            It scored {run.signals ?? 0} signal{run.signals === 1 ? '' : 's'} and drafted {run.drafted ?? 0} idea
            {run.drafted === 1 ? '' : 's'}
            {run.suppressed ? `, all ${run.suppressed} of them too close to something already in the queue` : ''}.
          </span>
        )}

        <span>
          {diagnosis.kind === 'filters' ? (
            <>
              Every filter that rejected something is named above, with the value it used —{' '}
              <Link to="/settings" className="underline underline-offset-4">
                loosen the one responsible in Settings
              </Link>
              .
            </>
          ) : (
            <>
              The hashtags decide what gets looked at at all —{' '}
              <Link to="/settings" className="underline underline-offset-4">
                change them in Settings
              </Link>
              .
            </>
          )}
        </span>
      </AlertDescription>
    </Alert>
  );
}
