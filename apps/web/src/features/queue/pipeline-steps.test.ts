import { describe, expect, it } from 'vitest';
import type { ProductionRow } from '@/lib/database.types';
import {
  availableControls,
  buildTimeline,
  currentGraphStep,
  describeProduction,
  isAtScriptGate,
  isRenderInFlight,
  isScriptEditable,
  isUnclaimed,
  missingSourceInput,
  needsSourceFootage,
  rewindableSteps,
  scriptLockReason,
  sourceGapReason,
  stoppedAtStep,
  WORKER_GRACE_MS,
} from './pipeline-steps';

type Prod = Parameters<typeof buildTimeline>[0];

function production(overrides: Partial<ProductionRow> = {}): Prod {
  return {
    status: 'running',
    stage: null,
    run_state: {},
    error: null,
    task_id: null,
    video_url: null,
    paused_at: null,
    superseded_by: null,
    lease_expires_at: null,
    ...overrides,
  } as Prod;
}

/** The state of one display step, by key. */
function states(prod: Prod): Record<string, string> {
  return Object.fromEntries(buildTimeline(prod).map((step) => [step.key, step.state]));
}

describe('where a production is', () => {
  it('treats a row the driver has not touched as still queued', () => {
    // `run_state` is empty for the few seconds between Gate 1 and the
    // dispatcher's next pass. That window is real and must not read as an
    // error or an empty page.
    const prod = production({ status: 'queued', run_state: {} });

    // The start step, which the script gate moved: a fresh production begins
    // by drafting words, not by spending money. `FIRST_STEP`, `graph.START`
    // and `resume_step()` in Postgres all carry this default and must agree.
    expect(currentGraphStep(prod)).toBe('write_script');
    expect(states(prod).queued).toBe('active');
    expect(describeProduction(prod).headline).toBe('Starting');
  });

  it('marks the steps before the current one as done', () => {
    const prod = production({ run_state: { step: 'generate_copy' } });

    expect(states(prod)).toMatchObject({
      queued: 'done',
      render: 'done',
      check: 'done',
      copy: 'active',
      gate2: 'pending',
      publish: 'pending',
    });
  });

  it('reads a poll step as its display step rather than as a stage string', () => {
    // `stage` here says "render 42%", which is free text with no constraint
    // behind it. The position comes from `run_state.step`.
    const prod = production({ run_state: { step: 'poll_render' }, stage: 'render 42%' });

    expect(states(prod).render).toBe('active');
    expect(describeProduction(prod).detail).toBe('render 42%');
  });
});

describe('a production that stopped', () => {
  it('places the failure at the step it actually failed on', () => {
    // The whole reason `previous_step` exists: `step` has been overwritten
    // with the terminal's own name, so without it the timeline would only be
    // able to say "parked" and could not say where.
    const prod = production({
      status: 'parked',
      error: 'HeyGen: avatar_not_found',
      run_state: { step: 'parked', previous_step: 'submit_render' },
    });

    expect(stoppedAtStep(prod)).toBe('submit_render');
    expect(states(prod).render).toBe('failed');
    expect(describeProduction(prod).headline).toBe('Stopped — needs you');
    expect(describeProduction(prod).detail).toBe('HeyGen: avatar_not_found');
  });

  it('falls back to the first step when a park predates previous_step', () => {
    const prod = production({ status: 'parked', run_state: { step: 'parked' } });

    expect(stoppedAtStep(prod)).toBe('write_script');
  });

  it('reads a reconciler park, which never overwrote the step at all', () => {
    // `supa.park` writes the status and the error but does not route, so the
    // step is still the real one.
    const prod = production({ status: 'parked', run_state: { step: 'poll_render' } });

    expect(stoppedAtStep(prod)).toBe('poll_render');
    expect(states(prod).render).toBe('failed');
  });
});

describe('publishing being switched off', () => {
  const held = production({
    status: 'approved',
    run_state: { step: 'publishing_disabled', previous_step: 'await_gate2' },
    video_url: 'https://x/final.mp4',
    task_id: 't1',
  });

  it('does not read as a failure', () => {
    // These rows keep `status = 'approved'` on purpose and hold a finished cut.
    // Calling it a failure would imply something needs fixing.
    const verdict = describeProduction(held);

    expect(verdict.tone).toBe('held');
    expect(verdict.headline).toContain('Held');
    expect(verdict.detail).toContain('Nothing is wrong');
  });

  it('shows publishing as skipped rather than pending', () => {
    expect(states(held)).toMatchObject({ gate2: 'done', publish: 'skipped', done: 'skipped' });
  });

  it('can still be re-run, because it has finished as far as it is going to', () => {
    expect(availableControls(held).rerun.enabled).toBe(true);
  });
});

describe('gate 2 waits on a person, not on the machine', () => {
  it('is waiting, not active', () => {
    const prod = production({ status: 'awaiting_review', run_state: { step: 'await_gate2' } });

    expect(states(prod).gate2).toBe('waiting');
    expect(describeProduction(prod).tone).toBe('waiting');
  });

  it('says a failed quality check can still be approved', () => {
    const prod = production({ status: 'qc_failed', run_state: { step: 'await_gate2' } });

    expect(describeProduction(prod).headline).toContain('quality check failed');
    expect(describeProduction(prod).detail).toContain('still approve');
  });
});

describe('a render that may already have been billed for', () => {
  it.each(['submit_render', 'poll_render', 'fetch_and_qc'])('counts %s with a task id', (step) => {
    expect(isRenderInFlight(production({ task_id: 't1', run_state: { step } }))).toBe(true);
  });

  it('does not count a step with no task id, because nothing was submitted', () => {
    expect(isRenderInFlight(production({ task_id: null, run_state: { step: 'submit_render' } }))).toBe(false);
  });

  it('does not count a step past the render', () => {
    expect(isRenderInFlight(production({ task_id: 't1', run_state: { step: 'generate_copy' } }))).toBe(false);
  });

  it('refuses a cancel and says why', () => {
    const control = availableControls(production({ task_id: 't1', run_state: { step: 'poll_render' } })).cancel;

    expect(control.enabled).toBe(false);
    expect(control.reason).toContain('already been billed');
  });

  it('still allows a pause, and warns that the provider carries on', () => {
    const prod = production({ task_id: 't1', run_state: { step: 'poll_render' } });

    expect(availableControls(prod).pause.enabled).toBe(true);
    expect(describeProduction({ ...prod, paused_at: '2026-09-07T10:00:00Z' }).detail).toContain('carries on');
  });
});

describe('which controls are offered', () => {
  it('offers retry only on a stopped production', () => {
    expect(availableControls(production({ status: 'parked' })).retry.enabled).toBe(true);
    expect(availableControls(production({ status: 'running' })).retry.enabled).toBe(false);
  });

  it('refuses everything but pause while the worker holds the lease', () => {
    // The SQL refuses a leased row rather than racing the driver for
    // `run_state`; this mirrors it so the owner is told, not just refused.
    const leased = production({
      status: 'parked',
      lease_expires_at: new Date(Date.now() + 60_000).toISOString(),
    });

    expect(availableControls(leased).retry.enabled).toBe(false);
    expect(availableControls(leased).retry.reason).toContain('moment');
    expect(availableControls(leased).pause.enabled).toBe(true);
  });

  it('ignores a lease that has already lapsed', () => {
    // An expired lease is the ordinary recovery path, not a busy worker.
    const stale = production({
      status: 'parked',
      lease_expires_at: new Date(Date.now() - 60_000).toISOString(),
    });

    expect(availableControls(stale).retry.enabled).toBe(true);
  });

  it('offers resume only when paused', () => {
    expect(availableControls(production()).resume.enabled).toBe(false);
    expect(availableControls(production({ paused_at: '2026-09-07T10:00:00Z' })).resume.enabled).toBe(true);
  });

  it('closes every control on a superseded production', () => {
    const superseded = production({ status: 'parked', superseded_by: 'p2' });
    const controls = availableControls(superseded);

    expect(controls.retry.enabled).toBe(false);
    expect(controls.rerun.enabled).toBe(false);
    expect(controls.cancel.enabled).toBe(false);
    expect(describeProduction(superseded).headline).toBe('Superseded');
  });

  it('will not re-run a production that is still going', () => {
    expect(availableControls(production({ status: 'running' })).rerun.enabled).toBe(false);
  });
});

describe('what a production can be rewound to', () => {
  it('offers nothing before a render exists', () => {
    expect(rewindableSteps(production())).toEqual([]);
  });

  it('offers only the quality check once there is a render but no cut', () => {
    expect(rewindableSteps(production({ task_id: 't1' }))).toEqual(['fetch_and_qc']);
  });

  it('offers the copy and the gate once there is a finished cut', () => {
    const prod = production({ task_id: 't1', video_url: 'https://x/final.mp4' });

    expect(rewindableSteps(prod)).toEqual(['fetch_and_qc', 'generate_copy', 'open_gate2']);
  });

  it('never offers a re-render, because that costs money and opens a new row', () => {
    const prod = production({ task_id: 't1', video_url: 'https://x/final.mp4' });

    expect(rewindableSteps(prod)).not.toContain('submit_render');
  });
});

// ---------------------------------------------------------------------------
// The script gate
// ---------------------------------------------------------------------------

describe('the script gate', () => {
  const atGate = () => production({ status: 'awaiting_script', run_state: { step: 'await_script' } });

  it('recognises a production waiting for its script to be approved', () => {
    expect(isAtScriptGate(atGate())).toBe(true);
    expect(isAtScriptGate(production({ status: 'running', run_state: { step: 'poll_render' } }))).toBe(false);
  });

  it('shows the gate as waiting for a person rather than as work in progress', () => {
    expect(states(atGate())).toMatchObject({ queued: 'done', script: 'waiting', render: 'pending' });
  });

  it('says in a sentence that nothing renders until the words are approved', () => {
    expect(describeProduction(atGate()).headline).toBe('Script — waiting for you');
    expect(describeProduction(atGate()).detail).toMatch(/nothing renders until you do/);
  });

  it('marks the gate done once the production has moved past it', () => {
    const prod = production({ status: 'running', run_state: { step: 'poll_render' }, task_id: 't1' });

    expect(states(prod)).toMatchObject({ script: 'done', render: 'active' });
  });

  it('shows a failed draft as a failure on the gate, not on the render', () => {
    const prod = production({
      status: 'parked',
      run_state: { step: 'parked', previous_step: 'write_script' },
      error: 'MptError: connection refused',
    });

    expect(states(prod)).toMatchObject({ script: 'failed', render: 'pending' });
  });
});

describe('whether the script is still an input to anything', () => {
  it('is editable while the production waits at the gate', () => {
    expect(isScriptEditable(production({ status: 'awaiting_script', run_state: { step: 'await_script' } }))).toBe(true);
  });

  it('is editable on a production parked before it reached the gate', () => {
    // The drafting service being down must not be a dead end: an owner can
    // write the words themselves, and `approve_script` accepts this row.
    const prod = production({ status: 'parked', run_state: { step: 'write_script' } });

    expect(isScriptEditable(prod)).toBe(true);
  });

  it('is not editable once a render has been submitted', () => {
    // `task_id` is set at the moment of submitting, so it is exactly the
    // "money may be moving" marker. Past it, the words have been paid for.
    const prod = production({ status: 'running', run_state: { step: 'await_script' }, task_id: 'p1' });

    expect(isScriptEditable(prod)).toBe(false);
    expect(scriptLockReason(prod)).toMatch(/already been submitted and paid for/);
  });

  it('is not editable past the gate even without a task id', () => {
    const prod = production({ status: 'running', run_state: { step: 'fetch_and_qc' } });

    expect(isScriptEditable(prod)).toBe(false);
    expect(scriptLockReason(prod)).toMatch(/past the script gate/);
  });

  it('is not editable on a superseded production', () => {
    const prod = production({ status: 'awaiting_script', run_state: { step: 'await_script' }, superseded_by: 'p2' });

    expect(isScriptEditable(prod)).toBe(false);
    expect(scriptLockReason(prod)).toMatch(/re-run/);
  });

  it('gives no reason when there is nothing to explain', () => {
    expect(scriptLockReason(production({ status: 'awaiting_script', run_state: { step: 'await_script' } }))).toBeNull();
  });
});

describe('a production nothing has picked up', () => {
  const opened = Date.parse('2026-09-07T10:00:00Z');
  const fresh = production({ status: 'queued', run_state: {}, created_at: '2026-09-07T10:00:00Z' });

  it('is still starting inside the grace period', () => {
    expect(isUnclaimed(fresh, opened + 5_000)).toBe(false);
    expect(describeProduction(fresh, opened + 5_000).headline).toBe('Starting');
  });

  it('stops promising a start once the grace has passed', () => {
    // The dispatcher polls every five seconds. Thirty seconds with no claim is
    // not a slow start; it is a worker that is not running, and saying
    // "within a few seconds" for hours is the bug this exists to prevent.
    const later = opened + WORKER_GRACE_MS + 1;

    expect(isUnclaimed(fresh, later)).toBe(true);
    const verdict = describeProduction(fresh, later);
    expect(verdict.headline).toBe('Waiting for a worker');
    expect(verdict.tone).toBe('waiting');
    expect(verdict.detail).toContain('not running');
  });

  it('does not count a row the worker has already touched', () => {
    const started = production({
      status: 'running',
      run_state: { step: 'write_script' },
      created_at: '2026-09-07T10:00:00Z',
    });
    expect(isUnclaimed(started, opened + 999_999)).toBe(false);
  });

  it('does not count a paused row, which is waiting on purpose', () => {
    const paused = production({
      status: 'queued',
      run_state: {},
      created_at: '2026-09-07T10:00:00Z',
      paused_at: '2026-09-07T10:00:01Z',
    });
    expect(isUnclaimed(paused, opened + 999_999)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// The footage lane
// ---------------------------------------------------------------------------

describe('what a footage lane still needs', () => {
  const ready = {
    source_video_key: 'sources/p1/1757000000000-clip.mp4',
    render_instruction: 'Cut this to thirty seconds and grade it warm.',
    source_consent_at: '2026-09-08T09:00:00Z',
  };

  it('asks for nothing on the four lanes that generate from text', () => {
    // Every mode but one, and the reason this returns null rather than throwing
    // for them: the check is asked on every production, not only this lane's.
    for (const mode of ['mpt', 'fal_visuals', 'fal_full', 'heygen'] as const) {
      expect(needsSourceFootage(mode)).toBe(false);
      expect(
        missingSourceInput({ source_video_key: null, render_instruction: null, source_consent_at: null }, mode),
      ).toBeNull();
    }
  });

  it('asks for footage, then the instruction, then consent — one at a time', () => {
    // The order matters: it is the same order `_missing_source_input` and
    // `approve_script` use, so an owner is walked through a checklist rather
    // than told a different thing each time they click.
    expect(
      missingSourceInput({ source_video_key: null, render_instruction: null, source_consent_at: null }, 'fal_video'),
    ).toBe('footage');
    expect(missingSourceInput({ ...ready, render_instruction: null, source_consent_at: null }, 'fal_video')).toBe(
      'instruction',
    );
    expect(missingSourceInput({ ...ready, source_consent_at: null }, 'fal_video')).toBe('consent');
    expect(missingSourceInput(ready, 'fal_video')).toBeNull();
  });

  it('does not accept whitespace as an instruction', () => {
    // `btrim(text)` with one argument strips only spaces, which is how a
    // newline once passed the script gate. Both sides name the whole set now.
    expect(missingSourceInput({ ...ready, render_instruction: '  \n  ' }, 'fal_video')).toBe('instruction');
  });

  it('waits rather than guessing while the style is unknown', () => {
    // A null mode is "the preset has not been read yet". Treating that as
    // "needs nothing" would enable the approve button on the one lane where it
    // must not be enabled, so `useSourceLane` reports `isLoading` separately
    // and the editor blocks on it.
    expect(needsSourceFootage(null)).toBe(false);
    expect(needsSourceFootage(undefined)).toBe(false);
  });

  it('gives each gap a reason an owner can act on', () => {
    expect(sourceGapReason('footage')).toMatch(/upload a video/i);
    expect(sourceGapReason('instruction')).toMatch(/instruction/i);
    expect(sourceGapReason('consent')).toMatch(/consent/i);
  });
});
