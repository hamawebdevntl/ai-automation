import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { ProductionTimeline } from '@/features/queue/components/production-timeline';
import type { ProductionEventRow } from '@/lib/database.types';
import { makeProduction } from '@/test/factories';

const production = makeProduction;

function event(overrides: Partial<ProductionEventRow> = {}): ProductionEventRow {
  return {
    id: crypto.randomUUID(),
    production_id: 'p1',
    step: 'poll_render',
    outcome: 'progress',
    detail: null,
    error: null,
    attempt: null,
    payload: {},
    actor_id: null,
    created_at: '2026-09-07T10:01:00Z',
    ...overrides,
  };
}

describe('ProductionTimeline', () => {
  it('draws every step, including the ones not reached yet', () => {
    // A step that only appears once it is running is a step nobody can ask a
    // question about beforehand.
    render(<ProductionTimeline production={production()} events={[]} />);

    for (const label of [
      'Queued',
      'Script — your approval',
      'Rendering',
      'Download and quality check',
      'Platform copy',
      'Gate 2 — your sign-off',
      'Publishing',
      'Finished',
    ]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
  });

  it('marks every gate as needing a person', () => {
    // Two of them now: the script gate before any spend, and Gate 2 after it.
    // Both are places the pipeline stops for a human rather than for a machine,
    // and the timeline says so in the same words.
    render(<ProductionTimeline production={production()} events={[]} />);
    expect(screen.getAllByText('needs a person')).toHaveLength(2);
  });

  it('shows the free-text stage as a sub-label of the active step', () => {
    // `stage` is unconstrained text the activities write. It is shown, but the
    // timeline's position never comes from it.
    render(
      <ProductionTimeline
        production={production({ run_state: { step: 'poll_render' }, stage: 'fal generating (kling)' })}
        events={[]}
      />,
    );

    expect(screen.getByText('fal generating (kling)')).toBeInTheDocument();
  });

  it('surfaces the error on a production that stopped', () => {
    render(
      <ProductionTimeline
        production={production({
          status: 'parked',
          error: 'HeyGenError: avatar_not_found',
          run_state: { step: 'parked', previous_step: 'submit_render' },
        })}
        events={[]}
      />,
    );

    expect(screen.getByText('HeyGenError: avatar_not_found')).toBeInTheDocument();
    expect(screen.getAllByText('Stopped').length).toBeGreaterThan(0);
  });

  it('keeps the technical record behind a disclosure rather than in the way', async () => {
    const user = userEvent.setup();
    render(
      <ProductionTimeline
        production={production({ run_state: { step: 'poll_render' } })}
        events={[
          event({
            outcome: 'retrying',
            attempt: 2,
            detail: 'Attempt 2 of 5 failed. Trying again in 60s.',
            error: 'MptQueueFull: 429',
          }),
        ]}
      />,
    );

    expect(screen.queryByText(/MptQueueFull/)).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /1 entry/ }));

    expect(screen.getByText('MptQueueFull: 429')).toBeInTheDocument();
    expect(screen.getByText('attempt 2')).toBeInTheDocument();
  });

  it('shows the provider handles that are the only way to find a paid render', async () => {
    const user = userEvent.setup();
    render(
      <ProductionTimeline
        production={production({ run_state: { step: 'poll_render' } })}
        events={[event({ outcome: 'progress', payload: { heygen_video_id: 'vid-9', progress: 40 } })]}
      />,
    );

    await user.click(screen.getByRole('button', { name: /1 entry/ }));

    expect(screen.getByText('heygen_video_id')).toBeInTheDocument();
    expect(screen.getByText('vid-9')).toBeInTheDocument();
  });

  it('files an owner action under the step the production is on', async () => {
    const user = userEvent.setup();
    render(
      <ProductionTimeline
        production={production({ status: 'parked', run_state: { step: 'parked', previous_step: 'fetch_and_qc' } })}
        events={[
          event({
            step: 'control',
            outcome: 'control',
            detail: 'Retried by an owner from fetch_and_qc.',
            actor_id: 'u1',
          }),
        ]}
      />,
    );

    await user.click(screen.getByRole('button', { name: /1 entry/ }));
    expect(screen.getByText('Retried by an owner from fetch_and_qc.')).toBeInTheDocument();
  });

  it('reads a held production as held rather than failed', () => {
    render(
      <ProductionTimeline
        production={production({
          status: 'approved',
          run_state: { step: 'publishing_disabled', previous_step: 'await_gate2' },
        })}
        events={[]}
      />,
    );

    // Publishing and the outcome are skipped; nothing reads as stopped.
    expect(screen.getAllByText('Skipped').length).toBe(2);
    expect(screen.queryByText('Stopped')).not.toBeInTheDocument();
  });

  it('shows render progress while the render is running', () => {
    render(
      <ProductionTimeline production={production({ run_state: { step: 'poll_render', progress: 42 } })} events={[]} />,
    );

    expect(screen.getByText('42%')).toBeInTheDocument();
  });

  it('counts the entries under each step separately', async () => {
    const user = userEvent.setup();
    render(
      <ProductionTimeline
        production={production({ run_state: { step: 'generate_copy' } })}
        events={[
          event({ step: 'submit_render', outcome: 'started' }),
          event({ step: 'poll_render', outcome: 'progress' }),
          event({ step: 'poll_render', outcome: 'succeeded' }),
        ]}
      />,
    );

    // Two of the three belong to the render step, which covers both
    // submit_render and poll_render.
    const trigger = screen.getByRole('button', { name: /3 entries/ });
    expect(trigger).toBeInTheDocument();
    await user.click(trigger);
    expect(within(document.body).getAllByText('done').length).toBeGreaterThan(0);
  });
});
