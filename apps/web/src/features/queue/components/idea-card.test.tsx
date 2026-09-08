import { render, screen } from '@testing-library/react';
import type { ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { makeIdea } from '@/test/factories';

vi.mock('@/features/auth/use-owner', () => ({
  useOwner: () => ({ isOwner: true, isLoading: false, role: 'owner', displayName: null }),
}));
vi.mock('@tanstack/react-router', () => ({
  Link: ({ children }: { children: ReactNode }) => <span>{children}</span>,
}));
vi.mock('@/features/queue/api', () => ({
  useRejectIdea: () => ({ mutateAsync: vi.fn(), isPending: false }),
}));

const { IdeaCard } = await import('@/features/queue/components/idea-card');

const scored = makeIdea({
  trend_run_id: 'search-1',
  relevance: 86,
  connection: 'It speaks to the parents you described.',
  rationale: 'quoting software is rising 3.4x against its own recent history.',
});

describe('the queue card', () => {
  it('shows what it always has, and nothing about fit', () => {
    render(<IdeaCard idea={scored} />);

    expect(screen.getByText('Why your quotes lose the job')).toBeInTheDocument();
    expect(screen.getByText(/rising/)).toBeInTheDocument();
    expect(screen.getByText('Review')).toBeInTheDocument();
    expect(screen.queryByText(/how it fits/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/strong fit/i)).not.toBeInTheDocument();
  });
});

describe('the search card', () => {
  it('adds why now, how it fits, and the fit', () => {
    render(<IdeaCard idea={scored} variant="search" />);

    expect(screen.getByText(/why now/i)).toBeInTheDocument();
    expect(screen.getByText(/rising 3.4x against/)).toBeInTheDocument();
    expect(screen.getByText(/how it fits/i)).toBeInTheDocument();
    expect(screen.getByText('It speaks to the parents you described.')).toBeInTheDocument();
    expect(screen.getByText(/strong fit/i)).toBeInTheDocument();
    expect(screen.getByText('86')).toBeInTheDocument();
  });

  it('omits what an idea does not have', () => {
    render(<IdeaCard idea={makeIdea({ rationale: null, connection: null, relevance: null })} variant="search" />);

    expect(screen.queryByText(/why now/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/how it fits/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/loose fit/i)).not.toBeInTheDocument();
  });
});
