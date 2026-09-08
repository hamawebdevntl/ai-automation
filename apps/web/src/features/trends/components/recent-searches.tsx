import { Link } from '@tanstack/react-router';
import { Button } from '@/components/ui/button';
import { describeRunStatus, EXAMPLE_PROMPTS, isSearchRun, truncatePrompt } from '@/features/trends/search';
import type { TrendRunRow } from '@/lib/database.types';
import { formatRelative } from '@/lib/format';

/**
 * The last few searches, newest first.
 *
 * Rows rather than chips: a prompt, when it ran, how it went and how many it
 * found is too much for a chip and exactly what decides whether a search is
 * worth going back to. Selecting one is a link and spends nothing -- it narrows
 * the queue to that search's ideas and puts its words back in the box.
 */
export function RecentSearches({ runs, selectedRunId }: { runs: TrendRunRow[]; selectedRunId: string | null }) {
  const searches = runs.filter(isSearchRun);
  if (searches.length === 0) return null;
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-2">
        <p className="text-sm font-medium">Recent searches</p>
        {selectedRunId && (
          <Link to="/queue" search={{ page: 1 }} className="text-xs underline underline-offset-4">
            Show the whole queue
          </Link>
        )}
      </div>
      <ul className="space-y-1.5">
        {searches.map((run) => (
          <li key={run.id}>
            <Link
              to="/queue"
              search={{ search: run.id }}
              aria-current={run.id === selectedRunId ? 'page' : undefined}
              className="flex flex-col gap-0.5 rounded-md border p-2.5 text-sm transition-colors hover:bg-accent/50 aria-[current=page]:border-primary/50 aria-[current=page]:bg-accent/40"
            >
              <span className="font-medium break-words">{truncatePrompt(run.prompt)}</span>
              <span className="text-xs text-muted-foreground">
                {formatRelative(run.requested_at)} · {describeRunStatus(run)}
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}

/**
 * The empty state: an invitation and two descriptions to start from.
 *
 * They fill the box and nothing else, and the line under them says so.
 * Spending a run is one explicit press of Search, whoever wrote the words.
 */
export function ExamplePrompts({ onPick, disabled = false }: { onPick: (prompt: string) => void; disabled?: boolean }) {
  return (
    <div className="space-y-2">
      <p className="text-sm font-medium">No searches yet</p>
      <p className="text-sm text-muted-foreground">
        Describe what you make and who it is for, or start from one of these:
      </p>
      <div className="flex flex-col gap-2 sm:flex-row sm:flex-wrap">
        {EXAMPLE_PROMPTS.map((prompt) => (
          <Button
            key={prompt}
            type="button"
            variant="outline"
            size="sm"
            disabled={disabled}
            onClick={() => onPick(prompt)}
            className="h-auto whitespace-normal text-left"
          >
            {prompt}
          </Button>
        ))}
      </div>
      <p className="text-xs text-muted-foreground">Examples only fill the box — nothing runs until you press Search.</p>
    </div>
  );
}
