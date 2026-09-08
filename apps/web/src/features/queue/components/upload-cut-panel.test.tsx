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

const uploadCut = vi.fn(async () => ({ id: 'p1' }));
let isPending = false;
vi.mock('@/features/queue/api', () => ({
  useUploadFinishedCut: () => ({ mutateAsync: uploadCut, isPending }),
}));

const { UploadCutPanel } = await import('@/features/queue/components/upload-cut-panel');

beforeEach(() => {
  vi.clearAllMocks();
  isPending = false;
  mockOwner.mockReturnValue({ isOwner: true, isLoading: false, role: 'owner', displayName: null });
});

/**
 * `delay: null` removes userEvent's inter-keystroke pause.
 *
 * Not a shortcut: the default delay is there to let debounced inputs settle,
 * and nothing in this form is debounced. With it, typing two sentences is tens
 * of seconds of wall clock — enough to push the whole parallel suite past its
 * per-test timeout.
 */
function setup() {
  return userEvent.setup({ delay: null });
}

function mp4(name = 'cut.mp4'): File {
  return new File([new Uint8Array([0, 1, 2, 3])], name, { type: 'video/mp4' });
}

async function fillIn(user: ReturnType<typeof userEvent.setup>, file: File | null = mp4()) {
  if (file) await user.upload(screen.getByLabelText('The cut'), file);
  await user.type(screen.getByLabelText('Title'), 'Three quoting mistakes');
  await user.type(screen.getByLabelText('What it is about'), 'The three things that lose the job.');
}

describe('UploadCutPanel', () => {
  it('says what an upload skips and what it does not', () => {
    render(<UploadCutPanel />);
    expect(screen.getByText(/skips idea approval, the script gate and the render/)).toBeInTheDocument();
  });

  it('tells a viewer an owner has to do this', () => {
    mockOwner.mockReturnValue({ isOwner: false, isLoading: false, role: 'viewer', displayName: null });
    render(<UploadCutPanel />);
    expect(screen.getByRole('button', { name: /Upload and check/ })).toBeDisabled();
    expect(screen.getByText('Read-only')).toBeInTheDocument();
  });

  it('says nothing is wrong before anything has been tried', () => {
    render(<UploadCutPanel />);
    expect(screen.queryByText(/A title is what the platform copy/)).not.toBeInTheDocument();
  });

  it('names every missing field at once rather than one at a time', async () => {
    const user = setup();
    render(<UploadCutPanel />);

    await user.click(screen.getByRole('button', { name: /Upload and check/ }));

    expect(screen.getByText(/Choose the finished cut/)).toBeInTheDocument();
    expect(screen.getByText(/A title is what the platform copy/)).toBeInTheDocument();
    expect(screen.getByText(/no script to write the captions from/)).toBeInTheDocument();
    expect(screen.getByText(/whether this video is AI-generated/)).toBeInTheDocument();
    expect(uploadCut).not.toHaveBeenCalled();
  });

  it('will not upload until the disclosure is answered', async () => {
    // The one field with no safe default. Everything else can be inferred from
    // the file; this cannot be inferred from anything.
    const user = setup();
    render(<UploadCutPanel />);
    await fillIn(user);

    await user.click(screen.getByRole('button', { name: /Upload and check/ }));

    expect(uploadCut).not.toHaveBeenCalled();
    expect(screen.getByText(/whether this video is AI-generated/)).toBeInTheDocument();
  });

  it('sends the file, the two texts and the disclosure', async () => {
    const user = setup();
    render(<UploadCutPanel />);
    await fillIn(user);
    await user.click(screen.getByRole('radio', { name: 'No' }));

    await user.click(screen.getByRole('button', { name: /Upload and check/ }));

    expect(uploadCut).toHaveBeenCalledWith({
      file: expect.objectContaining({ name: 'cut.mp4' }),
      title: 'Three quoting mistakes',
      brief: 'The three things that lose the job.',
      isAigc: false,
    });
  });

  it('records a yes as a yes', async () => {
    const user = setup();
    render(<UploadCutPanel />);
    await fillIn(user);
    await user.click(screen.getByRole('radio', { name: 'Yes' }));

    await user.click(screen.getByRole('button', { name: /Upload and check/ }));

    expect(uploadCut).toHaveBeenCalledWith(expect.objectContaining({ isAigc: true }));
  });

  it('empties the form afterwards, so the next upload is not the last one again', async () => {
    const user = setup();
    render(<UploadCutPanel />);
    await fillIn(user);
    await user.click(screen.getByRole('radio', { name: 'No' }));

    await user.click(screen.getByRole('button', { name: /Upload and check/ }));

    expect(toasts.success).toHaveBeenCalled();
    expect(screen.getByLabelText('Title')).toHaveValue('');
    expect(screen.getByLabelText('What it is about')).toHaveValue('');
  });

  it('says what went wrong rather than failing silently', async () => {
    uploadCut.mockRejectedValueOnce(new Error('new row violates row-level security policy'));
    const user = setup();
    render(<UploadCutPanel />);
    await fillIn(user);
    await user.click(screen.getByRole('radio', { name: 'No' }));

    await user.click(screen.getByRole('button', { name: /Upload and check/ }));

    expect(toasts.error).toHaveBeenCalledWith('Could not upload that cut', {
      description: 'new row violates row-level security policy',
    });
    // The draft survives a failure: retyping it would be the second insult.
    expect(screen.getByLabelText('Title')).toHaveValue('Three quoting mistakes');
  });

  it('warns that Instagram cannot add the music for you', () => {
    render(<UploadCutPanel />);
    expect(screen.getByText(/Instagram cannot attach licensed audio/)).toBeInTheDocument();
  });
});
