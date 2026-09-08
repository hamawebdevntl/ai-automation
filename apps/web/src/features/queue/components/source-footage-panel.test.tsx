import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { OwnerState } from '@/features/auth/use-owner';
import type { ProductionRow } from '@/lib/database.types';
import { makeProduction } from '@/test/factories';

/**
 * The footage lane's three inputs, from the owner's side.
 *
 * The assertions that matter here are not about layout. They are that each of
 * the three inputs is a *separate* write — so the upload cannot be mistaken for
 * an approval — and that consent cannot be recorded with a tick. Consent is the
 * one field on this screen that is answered to somebody outside the company,
 * and a one-word note would make the record worthless exactly when it is needed.
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

const attach = vi.fn(async () => ({ id: 'p1' }));
const clear = vi.fn(async () => ({ id: 'p1' }));
const saveInstruction = vi.fn(async () => ({ id: 'p1' }));
const confirmConsent = vi.fn(async () => ({ id: 'p1' }));

// `use-source-lane` imports from this module too, so the mock has to carry
// the query it reads even though `SourceFootagePanel` itself never calls it.
vi.mock('@/features/queue/api', () => ({
  stylePresetQueryOptions: () => ({ queryKey: ['style-preset'], queryFn: async () => null }),
  SOURCE_VIDEO_TYPES: ['video/mp4', 'video/quicktime', 'video/webm'],
  useAttachSourceVideo: () => ({ mutateAsync: attach, isPending: false }),
  useClearSourceVideo: () => ({ mutateAsync: clear, isPending: false }),
  useSaveRenderInstruction: () => ({ mutateAsync: saveInstruction, isPending: false }),
  useConfirmSourceConsent: () => ({ mutateAsync: confirmConsent, isPending: false }),
}));

const { SourceFootagePanel, formatBytes, validateInstruction } = await import(
  '@/features/queue/components/source-footage-panel'
);

/** A footage-lane production resting at the script gate. */
function atGate(overrides: Partial<ProductionRow> = {}): ProductionRow {
  return makeProduction({
    status: 'awaiting_script',
    run_state: { step: 'await_script' },
    render_backend: 'fal_video',
    ...overrides,
  });
}

const UPLOADED = {
  source_video_key: 'sources/p1/1757000000000-office.mp4',
  source_video_name: 'office.mp4',
  source_video_bytes: 42 * 1024 * 1024,
  source_video_uploaded_at: '2026-09-08T09:00:00Z',
} satisfies Partial<ProductionRow>;

const button = (name: RegExp | string) => screen.getByRole('button', { name });

beforeEach(() => {
  vi.clearAllMocks();
  mockOwner.mockReturnValue({ isOwner: true, isLoading: false, role: 'owner', displayName: null });
});

// ---------------------------------------------------------------------------

describe('the checklist', () => {
  it('says all three are still needed on a fresh production', () => {
    render(<SourceFootagePanel production={atGate()} />);

    expect(screen.getByText('Incomplete')).toBeInTheDocument();
    expect(screen.getAllByText('(still needed)')).toHaveLength(3);
  });

  it('says ready only once the footage, the instruction and consent are all there', () => {
    render(
      <SourceFootagePanel
        production={atGate({
          ...UPLOADED,
          render_instruction: 'Cut this to thirty seconds and grade it warm.',
          source_consent_at: '2026-09-08T09:10:00Z',
          source_consent_note: 'Filmed in our own office; everyone on camera is staff and agreed in writing.',
        })}
      />,
    );

    expect(screen.getByText('Ready')).toBeInTheDocument();
    expect(screen.queryByText('(still needed)')).not.toBeInTheDocument();
  });

  it('shows what is attached, so the owner can tell one upload from another', () => {
    render(<SourceFootagePanel production={atGate(UPLOADED)} />);

    expect(screen.getByText('office.mp4')).toBeInTheDocument();
    expect(screen.getByText(/42 MB/)).toBeInTheDocument();
  });
});

describe('uploading is not approving', () => {
  it('records the file on the production and nothing else', async () => {
    const user = userEvent.setup();
    render(<SourceFootagePanel production={atGate()} />);

    const file = new File(['x'], 'office.mp4', { type: 'video/mp4' });
    await user.upload(screen.getByLabelText('The video'), file);
    await user.click(button('Upload'));

    await waitFor(() => expect(attach).toHaveBeenCalledWith({ productionId: 'p1', file }));
    expect(saveInstruction).not.toHaveBeenCalled();
    expect(confirmConsent).not.toHaveBeenCalled();
  });

  it('says plainly that a replacement clears the approval and the consent', () => {
    // The one behaviour of this screen a person could be surprised by, so it
    // is on the page rather than only in the migration.
    render(<SourceFootagePanel production={atGate(UPLOADED)} />);

    expect(screen.getByText(/clears your approval and your consent record/)).toBeInTheDocument();
  });
});

describe('the instruction', () => {
  it('saves the words without approving anything', async () => {
    const user = userEvent.setup();
    render(<SourceFootagePanel production={atGate(UPLOADED)} />);

    await user.type(screen.getByLabelText('What to do with it'), 'Cut this to thirty seconds.');
    await user.click(button('Save the instruction'));

    await waitFor(() =>
      expect(saveInstruction).toHaveBeenCalledWith({
        productionId: 'p1',
        instruction: 'Cut this to thirty seconds.',
      }),
    );
  });

  it('cannot be saved empty', () => {
    render(<SourceFootagePanel production={atGate(UPLOADED)} />);

    expect(button('Save the instruction')).toBeDisabled();
  });

  it('holds the same limit the database does', () => {
    // 1500 is `productions_render_instruction_length`. The number agreeing is
    // not something you can see by looking at the page.
    expect(validateInstruction('').ok).toBe(false);
    expect(validateInstruction('   ').ok).toBe(false);
    expect(validateInstruction('a'.repeat(1500)).ok).toBe(true);
    expect(validateInstruction('a'.repeat(1501)).ok).toBe(false);
    expect(validateInstruction('a'.repeat(1501)).error).toMatch(/1,500/);
  });

  it('warns about a fragment without refusing it', () => {
    const verdict = validateInstruction('warmer');

    expect(verdict.ok).toBe(true);
    expect(verdict.warning).toMatch(/very short/);
  });
});

describe('consent', () => {
  it('cannot be recorded before there is footage to record it against', () => {
    render(<SourceFootagePanel production={atGate()} />);

    expect(screen.getByLabelText('Consent')).toBeDisabled();
    expect(button('Record consent')).toBeDisabled();
    expect(screen.getByText(/Upload the footage first/)).toBeInTheDocument();
  });

  it('refuses a one-word note, because a tick is not a record', async () => {
    const user = userEvent.setup();
    render(<SourceFootagePanel production={atGate(UPLOADED)} />);

    await user.type(screen.getByLabelText('Consent'), 'yes');

    expect(button('Record consent')).toBeDisabled();
    expect(confirmConsent).not.toHaveBeenCalled();
  });

  it('records a real note', async () => {
    const user = userEvent.setup();
    render(<SourceFootagePanel production={atGate(UPLOADED)} />);

    const note = 'Filmed in our own office; everyone on camera is staff and agreed in writing.';
    await user.type(screen.getByLabelText('Consent'), note);
    await user.click(button('Record consent'));

    await waitFor(() => expect(confirmConsent).toHaveBeenCalledWith({ productionId: 'p1', note }));
  });

  it('shows the note back once it is recorded, rather than a tick', () => {
    render(
      <SourceFootagePanel
        production={atGate({
          ...UPLOADED,
          source_consent_at: '2026-09-08T09:10:00Z',
          source_consent_note: 'Filmed in our own office; everyone on camera is staff.',
        })}
      />,
    );

    expect(screen.getByText('Filmed in our own office; everyone on camera is staff.')).toBeInTheDocument();
  });
});

describe('who may change any of it', () => {
  it('refuses a viewer', () => {
    mockOwner.mockReturnValue({ isOwner: false, isLoading: false, role: 'viewer', displayName: null });
    render(<SourceFootagePanel production={atGate(UPLOADED)} />);

    expect(screen.getByText('Read-only')).toBeInTheDocument();
    expect(screen.getByLabelText('The video')).toBeDisabled();
    expect(screen.getByLabelText('What to do with it')).toBeDisabled();
    expect(screen.getByLabelText('Consent')).toBeDisabled();
  });

  it('locks everything once the render has been paid for', () => {
    // `task_id` is set at the instant of submitting, so past it the footage and
    // the instruction are what was billed. Changing either would only make the
    // record wrong.
    render(
      <SourceFootagePanel
        production={atGate({ ...UPLOADED, task_id: 'p1', run_state: { step: 'poll_render' }, status: 'running' })}
      />,
    );

    expect(screen.getByText('Locked')).toBeInTheDocument();
    expect(screen.getByText(/already been submitted and paid for/)).toBeInTheDocument();
  });
});

describe('formatBytes', () => {
  it('reads in the unit a person would use', () => {
    expect(formatBytes(null)).toBe('size unknown');
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(2048)).toBe('2 KB');
    expect(formatBytes(5 * 1024 * 1024 + 512 * 1024)).toBe('5.5 MB');
    expect(formatBytes(120 * 1024 * 1024)).toBe('120 MB');
    expect(formatBytes(2 * 1024 * 1024 * 1024)).toBe('2.0 GB');
  });
});
