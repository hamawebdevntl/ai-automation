import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { OwnerState } from '@/features/auth/use-owner';
import type { SearchRun } from '@/features/trends/search';
import { trendSearchRun } from '@/features/trends/test-fixtures';
import type { TrendRunRow } from '@/lib/database.types';

const mockOwner = vi.fn<() => OwnerState>(() => ({
  isOwner: true,
  isLoading: false,
  role: 'owner',
  displayName: null,
}));
vi.mock('@/features/auth/use-owner', () => ({ useOwner: () => mockOwner() }));

const navigate = vi.fn(async () => {});
vi.mock('@tanstack/react-router', () => ({ useNavigate: () => navigate }));

const toasts = { success: vi.fn(), error: vi.fn(), info: vi.fn() };
vi.mock('sonner', () => ({ toast: toasts }));

const dismiss = vi.fn(async (): Promise<TrendRunRow> => trendSearchRun({ dismissed_at: '2026-09-08T12:00:00Z' }));
vi.mock('@/features/trends/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/features/trends/api')>();
  return { ...actual, useDismissTrendRun: () => ({ mutateAsync: dismiss, isPending: false }) };
});

const { RemoveSearchButton } = await import('@/features/trends/components/remove-search-button');

const finished = trendSearchRun({ status: 'succeeded', inserted: 4 }) as SearchRun;
const running = trendSearchRun({ status: 'running', inserted: null }) as SearchRun;

beforeEach(() => {
  vi.clearAllMocks();
  dismiss.mockImplementation(async () => trendSearchRun({ dismissed_at: '2026-09-08T12:00:00Z' }));
  mockOwner.mockReturnValue({ isOwner: true, isLoading: false, role: 'owner', displayName: null });
});

async function openAndConfirm(actionName: RegExp) {
  const user = userEvent.setup();
  await user.click(screen.getByRole('button', { name: /^remove “/i }));
  await screen.findByRole('alertdialog');
  await user.click(screen.getByRole('button', { name: actionName }));
}

describe('RemoveSearchButton', () => {
  it('shows a viewer nothing to press', () => {
    mockOwner.mockReturnValue({ isOwner: false, isLoading: false, role: 'viewer', displayName: null });
    const { container } = render(<RemoveSearchButton run={finished} selected={false} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('names the search it would remove', () => {
    render(<RemoveSearchButton run={finished} selected={false} />);
    expect(screen.getByRole('button', { name: /remove “i want to start a small home fitness/i })).toBeInTheDocument();
  });

  it('asks first, then removes by id and says the ideas stay', async () => {
    render(<RemoveSearchButton run={finished} selected={false} />);

    await openAndConfirm(/^remove$/i);

    await waitFor(() => expect(dismiss).toHaveBeenCalledWith('search-1'));
    expect(toasts.success).toHaveBeenCalledWith('Search removed', expect.anything());
    expect(navigate).not.toHaveBeenCalled();
  });

  it('leaves the filtered view when the removed search was the selected one', async () => {
    render(<RemoveSearchButton run={finished} selected={true} />);

    await openAndConfirm(/^remove$/i);

    await waitFor(() => expect(navigate).toHaveBeenCalledWith({ to: '/queue', search: { page: 1 } }));
  });

  it('says a running search is stopped as well as removed', async () => {
    // "Remove" pressed on something running means "and stop spending on it".
    render(<RemoveSearchButton run={running} selected={false} />);

    await openAndConfirm(/^stop and remove$/i);

    expect(screen.queryByText(/stop and remove this search\?/i)).not.toBeInTheDocument();
    await waitFor(() => expect(dismiss).toHaveBeenCalledWith('search-1'));
    expect(toasts.success).toHaveBeenCalledWith('Search stopped and removed', expect.anything());
  });

  it('treats an already-removed search as good news, not an error', async () => {
    dismiss.mockRejectedValueOnce({ code: 'P0002', message: 'That search has already been removed' });
    render(<RemoveSearchButton run={finished} selected={false} />);

    await openAndConfirm(/^remove$/i);

    await waitFor(() => expect(toasts.info).toHaveBeenCalled());
    expect(toasts.error).not.toHaveBeenCalled();
  });

  it('keeps the dialog open and says why on a real failure', async () => {
    dismiss.mockRejectedValueOnce(new Error('network is down'));
    render(<RemoveSearchButton run={finished} selected={false} />);

    await openAndConfirm(/^remove$/i);

    await waitFor(() => expect(toasts.error).toHaveBeenCalled());
    expect(screen.getByRole('alertdialog')).toBeInTheDocument();
  });
});
