import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import type { OwnerState } from '@/features/auth/use-owner';
import { StylePicker } from '@/features/queue/components/style-picker';
import type { StylePresetRow, StylePresetSpendRow } from '@/lib/database.types';

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
  render_mode: 'heygen',
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

  it('names the presenter engine, which is the most expensive one to pick by accident', () => {
    mockOwner.mockReturnValue({ isOwner: true, isLoading: false, role: 'owner', displayName: null });
    render(<StylePicker presets={[PRESENTER]} value={null} onChange={vi.fn()} />);
    expect(screen.getByText('HeyGen presenter')).toBeInTheDocument();
  });
});

describe('a style at its spend cap', () => {
  function spend(overrides: Partial<StylePresetSpendRow> = {}): StylePresetSpendRow {
    return {
      style_preset_id: 'preset-presenter',
      slug: 'ai-presenter',
      provider: 'heygen',
      model: '',
      block_reason: null,
      render_count: 0,
      measured_avg_usd: null,
      measured_min_usd: null,
      measured_max_usd: null,
      ...overrides,
    };
  }

  it('cannot be chosen, and says which cap was hit and when it resets', async () => {
    // The sentence is composed by `spend_block_reason()` in Postgres and shown
    // verbatim: one wording for the disabled button here and for the park
    // reason on a production that reached the render step.
    const onChange = vi.fn();
    const user = userEvent.setup();
    const reason = 'Daily spend cap reached for heygen: $30.00 of $30.00 today. It resets 2026-09-09 00:00 UTC.';

    render(
      <StylePicker
        presets={[preset(), PRESENTER]}
        spend={[spend({ block_reason: reason })]}
        value={null}
        onChange={onChange}
      />,
    );
    await user.click(screen.getByLabelText(/AI presenter/));

    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByText(reason)).toBeInTheDocument();
    expect(screen.getByText('Capped')).toBeInTheDocument();
  });

  it('leaves every other style pickable, because the cap is per provider', async () => {
    const onChange = vi.fn();
    const user = userEvent.setup();

    render(
      <StylePicker
        presets={[preset(), PRESENTER]}
        spend={[spend({ block_reason: 'Monthly spend cap reached for heygen.' })]}
        value={null}
        onChange={onChange}
      />,
    );
    await user.click(screen.getByLabelText(/Stock b-roll/));

    expect(onChange).toHaveBeenCalledWith('preset-stock');
  });

  it('marks nothing capped while the verdict is still loading', async () => {
    // Postgres is what refuses, so an unknown verdict must not disable a style
    // an owner is entitled to pick.
    const onChange = vi.fn();
    const user = userEvent.setup();

    render(<StylePicker presets={[PRESENTER]} spend={undefined} value={null} onChange={onChange} />);
    await user.click(screen.getByLabelText(/AI presenter/));

    expect(onChange).toHaveBeenCalledWith('preset-presenter');
    expect(screen.queryByText('Capped')).not.toBeInTheDocument();
  });
});

describe('measured cost replacing the estimate', () => {
  it('shows what the style has really cost once it has rendered', () => {
    // The acceptance criterion: the estimates get replaced by measured figures
    // once real renders exist. The presenter lane's $1-2 was, in its own
    // migration's words, "an estimate and not yet a measurement".
    render(
      <StylePicker
        presets={[PRESENTER]}
        spend={[
          {
            style_preset_id: 'preset-presenter',
            slug: 'ai-presenter',
            provider: 'heygen',
            model: '',
            block_reason: null,
            render_count: 4,
            measured_avg_usd: 1.42,
            measured_min_usd: 1.2,
            measured_max_usd: 1.6,
          },
        ]}
        value={null}
        onChange={vi.fn()}
      />,
    );

    expect(screen.getByText('$1.42 over 4 renders')).toBeInTheDocument();
    expect(screen.queryByText('$1.00 – $2.00')).not.toBeInTheDocument();
    // And says so, rather than letting a billed average pass for the estimate
    // it replaced.
    expect(screen.getByText(/actually billed, not an estimate/)).toBeInTheDocument();
    expect(screen.getByText(/The estimate was \$1\.00 – \$2\.00\./)).toBeInTheDocument();
  });

  it('keeps the estimate until something has actually been billed', () => {
    render(
      <StylePicker
        presets={[PRESENTER]}
        spend={[
          {
            style_preset_id: 'preset-presenter',
            slug: 'ai-presenter',
            provider: 'heygen',
            model: '',
            block_reason: null,
            render_count: 0,
            measured_avg_usd: null,
            measured_min_usd: null,
            measured_max_usd: null,
          },
        ]}
        value={null}
        onChange={vi.fn()}
      />,
    );

    expect(screen.getByText('$1.00 – $2.00')).toBeInTheDocument();
  });
});
