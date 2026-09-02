import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { StylePicker } from '@/features/queue/components/style-picker';
import type { StylePresetRow } from '@/lib/database.types';

function preset(overrides: Partial<StylePresetRow> = {}): StylePresetRow {
  return {
    id: 'preset-stock',
    slug: 'stock-broll',
    name: 'Stock b-roll',
    description: 'The default lane.',
    lane: 'stock',
    video_source: 'pexels',
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
