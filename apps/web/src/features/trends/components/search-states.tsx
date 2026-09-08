import { Link } from '@tanstack/react-router';
import { CircleAlertIcon, InfoIcon } from 'lucide-react';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { RunBreakdown } from '@/features/trends/components/run-breakdown';
import { formatTerm, type SearchRun } from '@/features/trends/search';
import type { TrendRunInterpretation } from '@/lib/database.types';
import { cn } from '@/lib/utils';

/**
 * How the worker read the description, said back.
 *
 * The restatement is the check: a misreading shows here, in one line, before
 * an hour of scouting is spent on it. The terms are what was actually looked
 * for, and the nudge is advice for the next description rather than a
 * complaint about this one -- the run went ahead regardless.
 */
export function SearchInterpretation({
  interpretation,
  className,
}: {
  interpretation: TrendRunInterpretation | null;
  className?: string;
}) {
  if (!interpretation) return null;
  const terms = interpretation.terms ?? [];
  return (
    <div className={cn('space-y-2', className)}>
      <p>
        Understood as: <span className="text-foreground">{interpretation.restatement}</span>
      </p>
      {terms.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {terms.map((term) => (
            <Badge key={term} variant="secondary" className="font-normal">
              {formatTerm(term, interpretation.vocabulary)}
            </Badge>
          ))}
        </div>
      )}
      {interpretation.vague && interpretation.nudge && (
        <p className="text-amber-700 dark:text-amber-400">Worth adding next time: {interpretation.nudge}</p>
      )}
    </div>
  );
}

/** The rephrasings the worker offered, as a list, or nothing. */
function Suggestions({ suggestions }: { suggestions: string[] }) {
  if (suggestions.length === 0) return null;
  return (
    <div className="space-y-1">
      <span>Worth trying:</span>
      <ul className="list-disc space-y-1 pl-5">
        {suggestions.map((suggestion) => (
          <li key={suggestion}>{suggestion}</li>
        ))}
      </ul>
    </div>
  );
}

/**
 * The way back into a search, from wherever its state is shown.
 *
 * Next to the box, refining means focusing it with the words still there. On
 * the page-wide banner, where the box may be describing something else, the
 * same state offers a link into the search instead.
 */
function Refine({
  run,
  onRefine,
  children,
}: {
  run: SearchRun;
  onRefine?: (prompt: string) => void;
  children: string;
}) {
  if (onRefine) {
    return (
      <Button variant="outline" size="sm" className="w-fit" onClick={() => onRefine(run.prompt)}>
        {children}
      </Button>
    );
  }
  return (
    <Button asChild variant="outline" size="sm" className="w-fit">
      <Link to="/queue" search={{ search: run.id }}>
        Open this search
      </Link>
    </Button>
  );
}

/**
 * A described search that added nothing.
 *
 * Deliberately not `diagnoseRun`'s language. That names the filter to loosen,
 * which is the right advice for the saved list and the wrong advice here: a
 * search that found nothing relevant is fixed by a better description, and the
 * suggestions are the worker's own attempt at one. The funnel is still under a
 * disclosure for anyone who wants to see whether the scrape itself was the
 * problem -- and when it plainly was, the banner says so instead.
 */
export function NothingRelevant({ run, onRefine }: { run: SearchRun; onRefine?: (prompt: string) => void }) {
  const interpretation = run.interpretation;
  const scrapeFailed = run.rejections !== null && run.rejections.seen === 0;
  return (
    <Alert variant={scrapeFailed ? 'destructive' : 'default'}>
      <CircleAlertIcon className="size-4" />
      <AlertTitle>Nothing relevant came back for that search</AlertTitle>
      <AlertDescription>
        <SearchInterpretation interpretation={interpretation} />
        <span>
          {scrapeFailed
            ? 'The source returned nothing at all for those terms, before anything was judged — so this is the search itself rather than the fit. Different wording, or trying again later, is the way out.'
            : 'It looked, but found nothing trending that connects to this.'}
        </span>
        <Suggestions suggestions={interpretation?.suggestions ?? []} />
        <Refine run={run} onRefine={onRefine}>
          Try again with more detail
        </Refine>
        {run.rejections && (
          <details className="w-full">
            <summary className="cursor-pointer text-xs underline underline-offset-4">
              Where the {run.rejections.seen.toLocaleString()} candidates went
            </summary>
            <div className="pt-3">
              <RunBreakdown run={run} />
            </div>
          </details>
        )}
      </AlertDescription>
    </Alert>
  );
}

/**
 * A described search that added only a few.
 *
 * Soft: the ideas are on the page, this is advice. A fuller description
 * usually finds more, and the suggestions say how.
 */
export function FewRelevant({ run, onRefine }: { run: SearchRun; onRefine?: (prompt: string) => void }) {
  const count = run.inserted ?? 0;
  return (
    <Alert>
      <InfoIcon className="size-4" />
      <AlertTitle>
        Only {count} idea{count === 1 ? '' : 's'} came back
      </AlertTitle>
      <AlertDescription>
        <SearchInterpretation interpretation={run.interpretation} />
        <span>{count === 1 ? 'It is' : 'They are'} listed below. A fuller description usually finds more.</span>
        <Suggestions suggestions={run.interpretation?.suggestions ?? []} />
        <Refine run={run} onRefine={onRefine}>
          Try again with more detail
        </Refine>
      </AlertDescription>
    </Alert>
  );
}

/**
 * A search that finished without ever being read.
 *
 * Only one way for this to happen: the bundle that can create a described run
 * reached a browser before the worker that knows how to read one was
 * restarted. The old worker claimed it, scouted the saved list, and whatever
 * it added answers a question nobody asked. Said plainly, because the ideas
 * below would otherwise look like the answer.
 */
export function NotInterpreted({ run }: { run: SearchRun }) {
  return (
    <Alert variant="destructive">
      <CircleAlertIcon className="size-4" />
      <AlertTitle>This search was not read as a search</AlertTitle>
      <AlertDescription>
        <span>
          It ran on a worker that predates searching by description, so it scouted the saved list instead of “
          {run.prompt}”. Anything it added is not an answer to that. Redeploy the worker and search again.
        </span>
      </AlertDescription>
    </Alert>
  );
}
