import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { OwnerState } from '@/features/auth/use-owner';
import type { ProductionRow } from '@/lib/database.types';
import { makeProduction } from '@/test/factories';

/**
 * The script gate, from the owner's side.
 *
 * The two assertions that matter most here are not about layout. One is that
 * *nothing but the approve button starts a render* — saving and redrafting must
 * never call `approve_script`, because that is the click that spends money. The
 * other is that a draft arriving over Realtime cannot overwrite what somebody
 * is typing, which is the one thing this component could do that a person
 * cannot undo.
 */

const mockOwner = vi.fn<() => OwnerState>(() => ({
  isOwner: true,
  isLoading: false,
  role: 'owner',
  displayName: null,
}));
vi.mock('@/features/auth/use-owner', () => ({ useOwner: () => mockOwner() }));

const toasts = { success: vi.fn(), error: vi.fn(), info: vi.fn() };
vi.mock('sonner', () => ({ toast: toasts }));

const save = vi.fn(async () => ({ id: 'p1' }));
const approve = vi.fn(async () => ({ id: 'p1' }));
const redraft = vi.fn(async () => ({ id: 'p1' }));

vi.mock('@/features/queue/api', () => ({
  useSaveScript: () => ({ mutateAsync: save, isPending: false }),
  useApproveScript: () => ({ mutateAsync: approve, isPending: false }),
  useRedraftScript: () => ({ mutateAsync: redraft, isPending: false }),
}));

const { ScriptEditor, estimateSeconds, validateScript } = await import('@/features/queue/components/script-editor');

const DRAFT = 'A property CRM makes you type every viewing twice, and the second one is always wrong.';

/** A production resting at the script gate with a draft waiting to be read. */
function atGate(overrides: Partial<ProductionRow> = {}): ProductionRow {
  return makeProduction({
    status: 'awaiting_script',
    run_state: { step: 'await_script' },
    script: DRAFT,
    script_updated_at: '2026-09-07T10:05:00Z',
    ...overrides,
  });
}

function show(production: ProductionRow) {
  return render(<ScriptEditor production={production} />);
}

const textarea = () => screen.getByLabelText('Narration') as HTMLTextAreaElement;
const button = (name: RegExp | string) => screen.getByRole('button', { name });

async function confirmDialog(user: ReturnType<typeof userEvent.setup>, name: RegExp) {
  await user.click(await screen.findByRole('button', { name }));
}

beforeEach(() => {
  vi.clearAllMocks();
  mockOwner.mockReturnValue({ isOwner: true, isLoading: false, role: 'owner', displayName: null });
});

// ---------------------------------------------------------------------------

describe('reading the draft', () => {
  it('shows the generated script in an editable box', () => {
    show(atGate());

    expect(textarea()).toHaveValue(DRAFT);
    expect(textarea()).toBeEnabled();
  });

  it('says the script is waiting for approval', () => {
    show(atGate());

    expect(screen.getByText('Waiting for your approval')).toBeInTheDocument();
    expect(screen.getByText(/No render can start until you approve it/)).toBeInTheDocument();
  });

  it('waits, rather than showing an empty box, while the draft is being written', () => {
    show(makeProduction({ status: 'running', run_state: { step: 'write_script' }, script: null }));

    expect(screen.getByText(/Writing the first draft/)).toBeInTheDocument();
  });

  it('offers to let you write it yourself when the pipeline could not', () => {
    // Parked at `write_script` because MoneyPrinterTurbo was unreachable.
    // `approve_script` accepts this row, so this is a real offer rather than
    // an apology -- the render does not need the drafting service.
    show(
      makeProduction({
        status: 'parked',
        run_state: { step: 'write_script' },
        script: null,
        error: 'MptError: connection refused',
      }),
    );

    expect(screen.getByText('No draft was written')).toBeInTheDocument();
    expect(screen.getByText(/connection refused/)).toBeInTheDocument();
    expect(textarea()).toBeEnabled();
  });
});

describe('nothing renders until the owner says so', () => {
  it('saving persists the words without approving them', async () => {
    const user = userEvent.setup();
    show(atGate());

    await user.clear(textarea());
    await user.type(textarea(), 'Words I wrote myself, which are long enough to be a real script.');
    await user.click(button('Save without approving'));

    expect(save).toHaveBeenCalledWith({
      productionId: 'p1',
      script: 'Words I wrote myself, which are long enough to be a real script.',
    });
    expect(approve).not.toHaveBeenCalled();
  });

  it('approving confirms first, because that is the click that spends money', async () => {
    const user = userEvent.setup();
    show(atGate());

    await user.click(button('Approve and start the render'));

    expect(approve).not.toHaveBeenCalled();
    expect(screen.getByText(/generating it costs money/)).toBeInTheDocument();
  });

  it('approving sends the text on screen, not the text on the row', async () => {
    // The edit and the approval are one call on purpose: an owner who types
    // and approves must not be able to leave a version behind.
    const user = userEvent.setup();
    show(atGate());

    await user.clear(textarea());
    await user.type(textarea(), 'The version I actually want rendered, at a believable length.');
    await user.click(button('Approve and start the render'));
    await confirmDialog(user, /^Approve and render$/);

    await waitFor(() =>
      expect(approve).toHaveBeenCalledWith({
        productionId: 'p1',
        script: 'The version I actually want rendered, at a believable length.',
      }),
    );
  });

  it('redrafting confirms and does not approve anything', async () => {
    const user = userEvent.setup();
    show(atGate());

    await user.click(button('Write another draft'));
    expect(screen.getByText(/generates no video/)).toBeInTheDocument();

    await confirmDialog(user, /^Write another$/);

    await waitFor(() => expect(redraft).toHaveBeenCalledWith({ productionId: 'p1' }));
    expect(approve).not.toHaveBeenCalled();
  });
});

describe('validation', () => {
  it('refuses an empty script', async () => {
    const user = userEvent.setup();
    show(atGate());

    await user.clear(textarea());

    expect(screen.getByText('A script cannot be empty.')).toBeInTheDocument();
    expect(button('Approve and start the render')).toBeDisabled();
    expect(button('Save without approving')).toBeDisabled();
  });

  it('refuses a script past the limit the database enforces', () => {
    const long = 'x'.repeat(5001);

    expect(validateScript(long).ok).toBe(false);
    expect(validateScript(long).error).toMatch(/limit is 5,000/);
  });

  it('accepts one exactly at the limit', () => {
    expect(validateScript('x'.repeat(5000)).ok).toBe(true);
  });

  it('warns about a very short script without refusing it', () => {
    const short = validateScript('Buy our thing.');

    expect(short.ok).toBe(true);
    expect(short.warning).toMatch(/very short/);
  });

  it('counts trimmed length, so whitespace is not a script', () => {
    expect(validateScript('   \n  ').ok).toBe(false);
  });

  it('estimates spoken length from the words', () => {
    // 150 words a minute, so 75 words is about half of one.
    expect(estimateSeconds(Array(75).fill('word').join(' '))).toBe(30);
    expect(estimateSeconds('')).toBe(0);
  });
});

describe('a live update never eats an edit', () => {
  it('adopts a new draft when the box is untouched', () => {
    const { rerender } = show(atGate({ script: 'The first draft.' }));

    rerender(<ScriptEditor production={atGate({ script: 'A better second draft.' })} />);

    expect(textarea()).toHaveValue('A better second draft.');
  });

  it('keeps what the owner typed and offers the new draft instead', async () => {
    // The failure this prevents: a redraft, or a worker writing the first
    // draft, landing over Realtime while somebody is mid-sentence.
    const user = userEvent.setup();
    const { rerender } = show(atGate({ script: 'The first draft.' }));

    await user.clear(textarea());
    await user.type(textarea(), 'My own words.');

    rerender(<ScriptEditor production={atGate({ script: 'A draft that arrived just now.' })} />);

    expect(textarea()).toHaveValue('My own words.');
    expect(screen.getByText('A new draft arrived while you were editing')).toBeInTheDocument();

    await user.click(button('Use the new draft'));
    expect(textarea()).toHaveValue('A draft that arrived just now.');
  });
});

describe('who may change it', () => {
  it('is read-only for a viewer', () => {
    mockOwner.mockReturnValue({ isOwner: false, isLoading: false, role: 'viewer', displayName: null });
    show(atGate());

    expect(screen.getByText('Read-only')).toBeInTheDocument();
    expect(textarea()).toBeDisabled();
    expect(button('Approve and start the render')).toBeDisabled();
  });

  it('locks once a render has been submitted and paid for', () => {
    show(
      makeProduction({
        status: 'running',
        run_state: { step: 'poll_render' },
        task_id: 'p1',
        script: DRAFT,
        script_approved_at: '2026-09-07T10:10:00Z',
        script_approved_by: 'u1',
      }),
    );

    // Twice: the badge on the header and the alert that explains it.
    expect(screen.getAllByText('Locked')).toHaveLength(2);
    expect(screen.getByText(/already been submitted and paid for/)).toBeInTheDocument();
    expect(textarea()).toBeDisabled();
    expect(screen.queryByRole('button', { name: 'Approve and start the render' })).not.toBeInTheDocument();
  });

  it('locks a superseded production and points at its replacement', () => {
    show(atGate({ superseded_by: 'p2' }));

    expect(screen.getByText(/Edit the script on the one that replaced it/)).toBeInTheDocument();
  });

  it('holds approval back while the worker has the row', () => {
    show(atGate({ lease_expires_at: new Date(Date.now() + 60_000).toISOString() }));

    expect(button('Approve and start the render')).toBeDisabled();
    expect(screen.getByText(/running a step on this production right now/)).toBeInTheDocument();
  });
});

describe('after approval', () => {
  it('says the render uses exactly this text', () => {
    show(atGate({ script_approved_at: '2026-09-07T10:10:00Z', script_approved_by: 'u1' }));

    expect(screen.getByText('Approved')).toBeInTheDocument();
    expect(screen.getByText(/The render uses exactly this text/)).toBeInTheDocument();
  });

  it('marks an edit as unsaved, so the gate is visibly shut again', async () => {
    const user = userEvent.setup();
    show(atGate({ script_approved_at: '2026-09-07T10:10:00Z', script_approved_by: 'u1' }));

    await user.type(textarea(), ' And one more line to change it.');

    expect(screen.getByText('Unsaved changes')).toBeInTheDocument();
    expect(screen.getByText(/Save keeps them; approve saves and starts the render/)).toBeInTheDocument();
  });
});
