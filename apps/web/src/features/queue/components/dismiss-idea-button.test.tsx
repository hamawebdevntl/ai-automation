import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { OwnerState } from '@/features/auth/use-owner';

const mockOwner = vi.fn<() => OwnerState>(() => ({
  isOwner: true,
  isLoading: false,
  role: 'owner',
  displayName: null,
}));
vi.mock('@/features/auth/use-owner', () => ({ useOwner: () => mockOwner() }));

const toasts = { success: vi.fn(), error: vi.fn(), info: vi.fn() };
vi.mock('sonner', () => ({ toast: toasts }));

const reject = vi.fn(async () => ({ id: 'idea-1' }));
let isPending = false;
vi.mock('@/features/queue/api', () => ({
  useRejectIdea: () => ({ mutateAsync: reject, isPending }),
}));

const { DISMISS_NOTE, DismissIdeaButton } = await import('@/features/queue/components/dismiss-idea-button');

beforeEach(() => {
  vi.clearAllMocks();
  isPending = false;
  mockOwner.mockReturnValue({ isOwner: true, isLoading: false, role: 'owner', displayName: null });
});

function open() {
  return render(<DismissIdeaButton ideaId="idea-1" title="Why your quotes lose the job" />);
}

describe('DismissIdeaButton', () => {
  it('names the idea it would throw out', () => {
    open();
    expect(screen.getByRole('button', { name: /Why your quotes lose the job/ })).toBeInTheDocument();
  });

  it('shows a viewer nothing to press', () => {
    mockOwner.mockReturnValue({ isOwner: false, isLoading: false, role: 'viewer', displayName: null });
    const { container } = open();
    expect(container).toBeEmptyDOMElement();
  });

  it('does not reject on the first click', async () => {
    const user = userEvent.setup();
    open();
    await user.click(screen.getByRole('button', { name: /Why your quotes lose the job/ }));
    expect(screen.getByRole('alertdialog')).toBeInTheDocument();
    expect(reject).not.toHaveBeenCalled();
  });

  it('leaves the idea alone when the confirmation is declined', async () => {
    const user = userEvent.setup();
    open();
    await user.click(screen.getByRole('button', { name: /Why your quotes lose the job/ }));
    await user.click(screen.getByRole('button', { name: 'Keep it' }));
    expect(reject).not.toHaveBeenCalled();
  });

  it('rejects with a note saying where the decision came from', async () => {
    const user = userEvent.setup();
    open();
    await user.click(screen.getByRole('button', { name: /Why your quotes lose the job/ }));
    await user.click(screen.getByRole('button', { name: 'Dismiss' }));

    expect(reject).toHaveBeenCalledWith({ ideaId: 'idea-1', note: DISMISS_NOTE });
    expect(toasts.success).toHaveBeenCalled();
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
  });

  it('keeps the dialog open when the call fails', async () => {
    // Half-dismissed is the worst outcome: the card is still in the list, so
    // the button that failed has to still be there.
    reject.mockRejectedValueOnce(new Error('Only an owner may reject an idea'));
    const user = userEvent.setup();
    open();
    await user.click(screen.getByRole('button', { name: /Why your quotes lose the job/ }));
    await user.click(screen.getByRole('button', { name: 'Dismiss' }));

    expect(toasts.error).toHaveBeenCalledWith('Only an owner may reject an idea');
    expect(screen.getByRole('alertdialog')).toBeInTheDocument();
  });
});
