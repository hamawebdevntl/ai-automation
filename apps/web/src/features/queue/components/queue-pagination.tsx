import { Link } from '@tanstack/react-router';
import { ChevronLeftIcon, ChevronRightIcon } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Pagination, PaginationContent, PaginationItem } from '@/components/ui/pagination';
import { cn } from '@/lib/utils';

/**
 * Which page numbers to show around the current one.
 *
 * Clamped at both ends so the strip keeps its width: near page 1 it runs
 * forwards, near the end it runs backwards, and the controls do not resize as
 * you move through them.
 */
export function pageWindow(current: number, total: number, span = 5): number[] {
  if (total <= span) return Array.from({ length: total }, (_, i) => i + 1);
  const half = Math.floor(span / 2);
  const end = Math.min(total, Math.max(current + half, span));
  const start = Math.max(1, end - span + 1);
  return Array.from({ length: end - start + 1 }, (_, i) => start + i);
}

/**
 * Page controls for the idea queue.
 *
 * The page lives in the URL rather than in state, so a particular page can be
 * linked, reloaded, and reached with the back button -- which matters here
 * because reviewing an idea navigates away and comes back.
 *
 * Built from `Button` and the router's `Link` rather than `PaginationLink`,
 * which renders a bare anchor: that would reload the whole app on every page
 * change. The surrounding `Pagination` primitives are kept for the nav/ul/li
 * semantics they carry.
 *
 * Numbered pages are for pointers and wide screens. On a phone they would be
 * a row of small targets, so below `sm` this collapses to the two controls
 * worth having and a plain statement of where you are.
 */
export function QueuePagination({ page, pageCount }: { page: number; pageCount: number }) {
  if (pageCount <= 1) return null;

  const previous = Math.max(1, page - 1);
  const next = Math.min(pageCount, page + 1);

  return (
    <Pagination className="justify-between sm:justify-center">
      <PaginationContent className="w-full justify-between gap-2 sm:w-auto sm:justify-center">
        <PaginationItem>
          <Button
            asChild={page > 1}
            variant="outline"
            size="sm"
            disabled={page === 1}
            aria-label="Previous page"
            className={cn(page === 1 && 'pointer-events-none opacity-50')}
          >
            {page > 1 ? (
              <Link to="/queue" search={{ page: previous }}>
                <ChevronLeftIcon className="size-4" />
                <span className="hidden sm:inline">Previous</span>
              </Link>
            ) : (
              <span>
                <ChevronLeftIcon className="size-4" />
                <span className="hidden sm:inline">Previous</span>
              </span>
            )}
          </Button>
        </PaginationItem>

        {/* Phones: where you are, in words. */}
        <PaginationItem className="sm:hidden">
          <span className="text-sm text-muted-foreground" aria-current="page">
            Page {page} of {pageCount}
          </span>
        </PaginationItem>

        {/* Wider: the numbers themselves. */}
        {pageWindow(page, pageCount).map((number) => (
          <PaginationItem key={number} className="hidden sm:block">
            <Button
              asChild={number !== page}
              size="icon"
              variant={number === page ? 'outline' : 'ghost'}
              aria-label={`Page ${number}`}
              aria-current={number === page ? 'page' : undefined}
              className={cn(number === page && 'pointer-events-none font-semibold')}
            >
              {number === page ? (
                <span>{number}</span>
              ) : (
                <Link to="/queue" search={{ page: number }}>
                  {number}
                </Link>
              )}
            </Button>
          </PaginationItem>
        ))}

        <PaginationItem>
          <Button
            asChild={page < pageCount}
            variant="outline"
            size="sm"
            disabled={page === pageCount}
            aria-label="Next page"
            className={cn(page === pageCount && 'pointer-events-none opacity-50')}
          >
            {page < pageCount ? (
              <Link to="/queue" search={{ page: next }}>
                <span className="hidden sm:inline">Next</span>
                <ChevronRightIcon className="size-4" />
              </Link>
            ) : (
              <span>
                <span className="hidden sm:inline">Next</span>
                <ChevronRightIcon className="size-4" />
              </span>
            )}
          </Button>
        </PaginationItem>
      </PaginationContent>
    </Pagination>
  );
}
