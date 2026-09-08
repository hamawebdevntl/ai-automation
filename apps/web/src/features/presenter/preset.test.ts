import { describe, expect, it } from 'vitest';
import { presenterOf } from '@/features/presenter/preset';
import { presenterPreset } from '@/features/presenter/test-fixtures';

/**
 * `params` is `Json`, which is the honest type for a column nothing validates
 * column-side. Reading it therefore needs narrowing, and the cases below are
 * all shapes a real row has held: the seeded MoneyPrinterTurbo VideoParams
 * before the lane existed, and a preset on another lane entirely.
 */
describe('presenterOf', () => {
  it('reads the pair the preset names', () => {
    expect(presenterOf(presenterPreset())).toEqual({
      avatar_id: 'e6e4d0f4b4704568b32e8de751179c83',
      voice_id: '506420c8af914cb6a3cc3c350ccb411d',
    });
  });

  it('carries an engine only when one is written down', () => {
    const preset = presenterPreset({
      params: { heygen: { avatar_id: 'a1', voice_id: 'v1', engine: 'avatar_iii' } },
    });
    expect(presenterOf(preset)).toHaveProperty('engine', 'avatar_iii');
  });

  it('is null for a preset with no heygen block at all', () => {
    expect(presenterOf(presenterPreset({ params: { video_aspect: '9:16', font_size: 56 } }))).toBeNull();
  });

  it('is null when there is a voice but no avatar', () => {
    // The render step raises without an avatar id, so this names no presenter
    // — reporting half a pair would show the settings card as configured.
    expect(presenterOf(presenterPreset({ params: { heygen: { voice_id: 'v1' } } }))).toBeNull();
  });

  it('survives a params column that is not an object', () => {
    expect(presenterOf(presenterPreset({ params: null }))).toBeNull();
    expect(presenterOf(presenterPreset({ params: [] }))).toBeNull();
    expect(presenterOf(null)).toBeNull();
  });
});
