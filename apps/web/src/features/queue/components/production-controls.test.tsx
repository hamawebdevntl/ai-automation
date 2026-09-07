import { fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { OwnerState } from '@/features/auth/use-owner';
import type { ProductionRow } from '@/lib/database.types';
import { makeProduction } from '@/test/factories';

const mockOwner = vi.fn<() => OwnerState>(() => ({
  isOwner: true,
  isLoading: false,
  role: 'owner',
  displayName: null,
}));
vi.mock('@/features/auth/use-owner', () => ({ useOwner: () => mockOwner() }));

const toasts = { success: vi.fn(), error: vi.fn(), info: vi.fn() };
vi.mock('sonner', () => ({ toast: toasts }));

const pause = vi.fn(async () => ({ id: 'p1' }));
const resume = vi.fn(async () => ({ id: 'p1' }));
const retry = vi.fn(async () => ({ id: 'p1' }));
const cancel = vi.fn(async () => ({ id: 'p1' }));
const rerun = vi.fn(async () => ({ id: 'p2-aaaaaaaa' }));
const rewind = vi.fn(async () => ({ id: 'p1' }));

vi.mock('@/features/queue/api', () => ({
  usePauseProduction: () => ({ mutateAsync: pause, isPending: false }),
  useResumeProduction: () => ({ mutateAsync: resume, isPending: false }),
  useRetryProduction: () => ({ mutateAsync: retry, isPending: false }),
  useCancelProduction: () => ({ mutateAsync: cancel, isPending: false }),
  useRerunProduction: () => ({ mutateAsync: rerun, isPending: false }),
  useRewindProduction: () => ({ mutateAsync: rewind, isPending: false }),
}));

const { ProductionControls } = await import('@/features/queue/components/production-controls');

const production = makeProduction;

function show(overrides: Partial<ProductionRow> = {}) {
  return render(<ProductionControls production={production(overrides)} presets={[]} />);
}

beforeEach(() => {
  vi.clearAllMocks();
  mockOwner.mockReturnValue({ isOwner: true, isLoading: false, role: 'owner', displayName: null });
});

describe('who may drive a production', () => {
  it('tells a viewer why they cannot', () => {
    mockOwner.mockReturnValue({ isOwner: false, isLoading: false, role: 'viewer', displayName: null });
    show();

    expect(screen.getByText('Read-only')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Pause' })).toHaveAttribute('aria-disabled', 'true');
  });
});

describe('the actions that cannot be undone confirm first', () => {
  it('does not cancel on the first click', async () => {
    const user = userEvent.setup();
    show({ status: 'parked' });

    await user.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(screen.getByRole('alertdialog')).toBeInTheDocument();
    expect(cancel).not.toHaveBeenCalled();
  });

  it('cancels once confirmed', async () => {
    const user = userEvent.setup();
    show({ status: 'parked' });

    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    await user.click(screen.getByRole('button', { name: 'Cancel it' }));

    expect(cancel).toHaveBeenCalledWith({ productionId: 'p1', note: '' });
  });

  it('says plainly that re-running spends money again', async () => {
    const user = userEvent.setup();
    show({ status: 'parked' });

    await user.click(screen.getByRole('button', { name: 'Make it again' }));

    expect(screen.getByRole('alertdialog')).toHaveTextContent('It spends money');
    expect(screen.getByRole('alertdialog')).toHaveTextContent('kept exactly as it is');
  });

  it('promises a retry does not resubmit a paid render', async () => {
    const user = userEvent.setup();
    show({ status: 'parked', task_id: 't1', run_state: { step: 'parked', previous_step: 'poll_render' } });

    await user.click(screen.getByRole('button', { name: 'Retry' }));

    expect(screen.getByRole('alertdialog')).toHaveTextContent('reused rather than submitted again');
  });
});

describe('a control the database would refuse is not offered as if it would work', () => {
  it('will not cancel while a render has already been paid for', () => {
    show({ task_id: 't1', run_state: { step: 'poll_render' } });

    const button = screen.getByRole('button', { name: 'Cancel' });
    expect(button).toHaveAttribute('aria-disabled', 'true');

    // `fireEvent` rather than `userEvent`: this button is wrapped in a tooltip
    // trigger, and userEvent's full pointer sequence spends its time opening
    // and closing the tooltip instead of testing the thing under test, which
    // is that the click handler refuses to act on a refused control.
    fireEvent.click(button);

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(cancel).not.toHaveBeenCalled();
  });

  it('offers pause instead, and warns the provider carries on', async () => {
    const user = userEvent.setup();
    show({ task_id: 't1', run_state: { step: 'poll_render' } });

    await user.click(screen.getByRole('button', { name: 'Pause' }));

    expect(screen.getByRole('alertdialog')).toHaveTextContent('carries on regardless');
    expect(screen.getByRole('alertdialog')).toHaveTextContent('does not stop it');
  });

  it('will not retry a production that is running normally', () => {
    show({ status: 'running' });
    expect(screen.getByRole('button', { name: 'Retry' })).toHaveAttribute('aria-disabled', 'true');
  });

  it('keeps the refused button reachable, so the reason can be read', () => {
    // `aria-disabled`, not `disabled`: a truly disabled button takes no focus,
    // which would hide the explanation from keyboard users entirely.
    show({ task_id: 't1', run_state: { step: 'poll_render' } });

    expect(screen.getByRole('button', { name: 'Cancel' })).not.toHaveAttribute('disabled');
  });
});

describe('redoing a step', () => {
  it('offers nothing before there is a render', () => {
    show();
    expect(screen.queryByText('Redo a step')).not.toBeInTheDocument();
  });

  it('offers only the quality check when there is a render but no cut', () => {
    show({ task_id: 't1', status: 'parked' });

    expect(screen.getByRole('button', { name: /Re-check the render/ })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Rewrite the platform copy/ })).not.toBeInTheDocument();
  });

  it('rewinds to the step chosen', async () => {
    const user = userEvent.setup();
    show({ task_id: 't1', video_url: 'https://x/final.mp4', status: 'awaiting_review' });

    await user.click(screen.getByRole('button', { name: /Rewrite the platform copy/ }));
    await user.click(screen.getByRole('button', { name: 'Do it' }));

    expect(rewind).toHaveBeenCalledWith({ productionId: 'p1', note: '', step: 'generate_copy' });
  });

  it('says these cost nothing, because they reuse the render', () => {
    show({ task_id: 't1', status: 'parked' });
    expect(screen.getByText(/cost nothing/)).toBeInTheDocument();
  });
});

describe('the note travels with the action', () => {
  it('sends what the owner typed', async () => {
    const user = userEvent.setup();
    show({ status: 'parked' });

    await user.type(screen.getByLabelText(/Note/), 'wallet was empty');
    await user.click(screen.getByRole('button', { name: 'Retry' }));
    await user.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Retry' }));

    expect(retry).toHaveBeenCalledWith({ productionId: 'p1', note: 'wallet was empty' });
  });
});

describe('when the database refuses', () => {
  it('shows the reason the function gave rather than a generic failure', async () => {
    // `toError` in the api layer is what makes this true: supabase-js hands
    // back a plain object, not an Error, so without it every refusal fell
    // through to a generic message and threw away the explanation.
    const user = userEvent.setup();
    retry.mockRejectedValueOnce(new Error('The worker is running a step on this production right now'));
    show({ status: 'parked' });

    await user.click(screen.getByRole('button', { name: 'Retry' }));
    await user.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: 'Retry' }));

    expect(toasts.error).toHaveBeenCalledWith('The worker is running a step on this production right now');
  });
});
