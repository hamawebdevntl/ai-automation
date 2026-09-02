import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import type { OwnerState } from '@/features/auth/use-owner';
import { StylePicker } from '@/features/queue/components/style-picker';
import type { StylePresetRow } from '@/lib/database.types';

// StylePicker now shows an owner-only badge naming the render backend, so the
// role hook has to be controllable from the test.
const mockOwner = vi.fn<() => OwnerState>(() => ({
  isOwner: true,
  isLoading: false,
  role: 'owner',
  displayName: null,
}));
vi.mock('@/features/auth/use-owner', () => ({ useOwner: () => mockOwner() }));

function preset(overrides: Partial<StylePresetRow> = {}): StylePresetRow {
  return {
    id: 'preset-stock',
    slug: 'stock-broll',
    name: 'Stock b-roll',
    description: 'The default lane.',
    lane: 'stock',
    video_source: 'pexels',
    render_mode: 'mpt',
    est_cost_min_usd: 0.1,
    est_cost_max_usd: 0.4,
    est_minutes: 6,
    params: {},
    is_active: true,
    sort_order: 10,
    created_at: '2026-09-01T00:00:00Z',
    ...overrides,
  };
}

const PRESENTER = preset({
  id: 'preset-presenter',
  slug: 'ai-presenter',
  name: 'AI presenter',
  lane: 'presenter',
  video_source: 'heygen',
  est_cost_min_usd: 1,
  est_cost_max_usd: 2,
  est_minutes: 18,
});

describe('StylePicker', () => {
  it('shows the cost and ETA beside every choice, because this is where the money is committed', () => {
    render(<StylePicker presets={[preset(), PRESENTER]} value={null} onChange={() => {}} />);

    expect(screen.getByText('$0.10 – $0.40')).toBeInTheDocument();
    expect(screen.getByText('~6 min')).toBeInTheDocument();
    expect(screen.getByText('$1.00 – $2.00')).toBeInTheDocument();
    expect(screen.getByText('~18 min')).toBeInTheDocument();
  });

  it('reports the chosen preset by id', async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();

    render(<StylePicker presets={[preset(), PRESENTER]} value={null} onChange={onChange} />);
    await user.click(screen.getByLabelText(/AI presenter/));

    expect(onChange).toHaveBeenCalledWith('preset-presenter');
  });

  it('accepts no input while a decision is in flight', async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();

    render(<StylePicker presets={[preset()]} value={null} onChange={onChange} disabled />);
    await user.click(screen.getByLabelText(/Stock b-roll/));

    expect(onChange).not.toHaveBeenCalled();
  });
});

describe('render backend visibility', () => {
  const FAL = preset({
    id: 'preset-fal',
    slug: 'fal-generative',
    name: 'Generative (fal)',
    lane: 'generative',
    video_source: 'fal',
    render_mode: 'fal_visuals',
  });

  it('tells an owner which engine will run, because two presets share "fal"', () => {
    mockOwner.mockReturnValue({ isOwner: true, isLoading: false, role: 'owner', displayName: null });
    render(<StylePicker presets={[FAL]} value={null} onChange={vi.fn()} />);
    expect(screen.getByText('fal footage, standard assembly')).toBeInTheDocument();
  });

  it('hides the engine from a viewer, who cannot act on it', () => {
    mockOwner.mockReturnValue({ isOwner: false, isLoading: false, role: 'viewer', displayName: null });
    render(<StylePicker presets={[FAL]} value={null} onChange={vi.fn()} />);
    expect(screen.queryByText('fal footage, standard assembly')).not.toBeInTheDocument();
    // The lane badge is not operational detail and stays visible.
    expect(screen.getByText('fal')).toBeInTheDocument();
  });

  it('does not label the standard render, which needs no explanation', () => {
    mockOwner.mockReturnValue({ isOwner: true, isLoading: false, role: 'owner', displayName: null });
    render(<StylePicker presets={[preset()]} value={null} onChange={vi.fn()} />);
    expect(screen.queryByText('Standard render')).not.toBeInTheDocument();
  });
});
