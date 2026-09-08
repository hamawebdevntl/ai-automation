import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { makeClipCandidate } from '@/test/factories';

/**
 * The clip gate, from the owner's side.
 *
 * The assertions that matter here are not about layout. They are:
 *
 *   * **Accepting is the only thing that queues a production**, and a viewer
 *     cannot reach it. Discarding must never call `accept_clip_candidate`.
 *   * **A decided candidate offers no decision.** The SQL refuses a second one
 *     — `accept_clip_candidate` raises for a candidate that is not pending —
 *     but a button that produces an error toast is a button that should not
 *     have been there.
 *   * **The words are shown.** The excerpt is what the owner will be asked to
 *     approve at the script gate and what gets burned in as captions, so
 *     deciding without seeing it would be deciding on the model's summary.
 */

const accept = vi.fn();
const discard = vi.fn();

vi.mock('@/features/clips/api', () => ({
  useAcceptClipCandidate: () => ({ mutate: accept, isPending: false }),
  useDiscardClipCandidate: () => ({ mutate: discard, isPending: false }),
}));

// The preview signs a Storage URL and mounts a <video>; none of these tests is
// about either, and jsdom has no media stack to run it on.
vi.mock('@/features/clips/components/clip-preview', () => ({
  ClipPreview: ({ start, end }: { start: number; end: number }) => (
    <div data-testid="preview" data-start={start} data-end={end} />
  ),
}));

const toasts = { success: vi.fn(), error: vi.fn(), info: vi.fn() };
vi.mock('sonner', () => ({ toast: toasts }));

const { CandidateCard } = await import('@/features/clips/components/candidate-card');

const KEY = 'sources/clips/9f2b/webinar.mp4';

beforeEach(() => {
  vi.clearAllMocks();
});

describe('a pending candidate', () => {
  it('shows the range, the reason and the words that will be said', () => {
    render(<CandidateCard candidate={makeClipCandidate()} storageKey={KEY} canDecide />);

    expect(screen.getByText('The quote that went quiet')).toBeInTheDocument();
    // 100s -> 1:40, 112s -> 1:52, and the length beside it.
    expect(screen.getByText(/1:40\s*→\s*1:52/)).toBeInTheDocument();
    expect(screen.getByText('12s')).toBeInTheDocument();
    expect(screen.getByText(/stands alone/i)).toBeInTheDocument();
    expect(screen.getByText(/Your quote went out on Friday/)).toBeInTheDocument();
  });

  it('previews the recording at the clip start, not at the beginning', () => {
    render(<CandidateCard candidate={makeClipCandidate()} storageKey={KEY} canDecide />);

    const preview = screen.getByTestId('preview');
    expect(preview).toHaveAttribute('data-start', '100');
    expect(preview).toHaveAttribute('data-end', '112');
  });

  it('accepts, and only accepts, when the owner says make it', async () => {
    render(<CandidateCard candidate={makeClipCandidate()} storageKey={KEY} canDecide />);

    await userEvent.click(screen.getByRole('button', { name: /make this clip/i }));

    expect(accept).toHaveBeenCalledWith({ candidateId: 'cand-1' }, expect.anything());
    expect(discard).not.toHaveBeenCalled();
  });

  it('discards without ever calling accept', async () => {
    render(<CandidateCard candidate={makeClipCandidate()} storageKey={KEY} canDecide />);

    await userEvent.click(screen.getByRole('button', { name: /discard/i }));

    expect(discard).toHaveBeenCalledWith({ candidateId: 'cand-1' }, expect.anything());
    expect(accept).not.toHaveBeenCalled();
  });

  it('says that accepting does not itself start a render', () => {
    render(<CandidateCard candidate={makeClipCandidate()} storageKey={KEY} canDecide />);
    expect(screen.getByText(/approve the words before anything renders/i)).toBeInTheDocument();
  });

  it('offers a viewer no decision, and says why', () => {
    render(<CandidateCard candidate={makeClipCandidate()} storageKey={KEY} canDecide={false} />);

    expect(screen.queryByRole('button', { name: /make this clip/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /discard/i })).not.toBeInTheDocument();
    expect(screen.getByText(/only an owner can decide/i)).toBeInTheDocument();
  });
});

describe('a candidate that has been decided', () => {
  it('shows the acceptance and offers no second decision', () => {
    render(
      <CandidateCard
        candidate={makeClipCandidate({
          decision: 'accepted',
          decided_at: '2026-09-08T12:00:00Z',
          decided_by: 'owner-1',
          idea_id: 'idea-clip-1',
        })}
        storageKey={KEY}
        canDecide
      />,
    );

    expect(screen.getByText(/accepted/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /make this clip/i })).not.toBeInTheDocument();
  });

  it('shows a discard and offers no second decision', () => {
    render(
      <CandidateCard
        candidate={makeClipCandidate({
          decision: 'discarded',
          decided_at: '2026-09-08T12:00:00Z',
          decided_by: 'owner-1',
        })}
        storageKey={KEY}
        canDecide
      />,
    );

    expect(screen.getByText(/discarded/i)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /discard/i })).not.toBeInTheDocument();
  });

  it('does not fetch the recording to preview a settled clip', () => {
    render(
      <CandidateCard
        candidate={makeClipCandidate({
          decision: 'discarded',
          decided_at: '2026-09-08T12:00:00Z',
          decided_by: 'owner-1',
        })}
        storageKey={KEY}
        canDecide
      />,
    );

    expect(screen.queryByTestId('preview')).not.toBeInTheDocument();
  });
});
