import { describe, expect, it } from 'vitest';
import {
  blockedReason,
  byUsability,
  DEFAULT_ENGINE,
  engineFor,
  needsConfirmation,
  orientationWarning,
  toChoice,
} from '@/features/presenter/presenter';
import { look, voice } from '@/features/presenter/test-fixtures';

/**
 * The two rules that decide whether a pick renders or parks.
 *
 * Both fail terminally at HeyGen — `avatar_not_found` and a rejected engine
 * are in TERMINAL_CODES — which means after Gate 1, having already spent the
 * review. Postgres refuses the same cases and is what actually holds; these
 * are what makes the page say so before the button is pressed.
 */

describe('engineFor', () => {
  it('sends nothing when the look advertises the default, because omitting it selects Avatar IV', () => {
    expect(engineFor(look({ engines: ['avatar_iii', DEFAULT_ENGINE] }))).toBeNull();
  });

  it('names the engine a studio avatar does advertise', () => {
    // The documented failure: a look advertising avatar_iii only, rendered on
    // the engine it never claimed to support.
    expect(engineFor(look({ engines: ['avatar_iii'] }))).toBe('avatar_iii');
  });

  it('sends nothing when the response said nothing about engines', () => {
    // Empty is "no opinion", not "supports none". A field HeyGen renames must
    // cost the picker a check, not its ability to save anything at all.
    expect(engineFor(look({ engines: [] }))).toBeNull();
  });
});

describe('orientationWarning', () => {
  it('says nothing about a portrait look', () => {
    expect(orientationWarning(look({ orientation: 'portrait' }))).toBeNull();
  });

  it('warns that a landscape look crops the speaker', () => {
    expect(orientationWarning(look({ orientation: 'landscape' }))).toMatch(/crops the speaker/);
  });

  it('says so when HeyGen did not report the framing', () => {
    // Silence here would suppress the warning on exactly the looks least is
    // known about, which is the wrong way round.
    expect(orientationWarning(look({ orientation: 'unknown' }))).toMatch(/did not say/);
  });

  it('asks for confirmation only where the crop is severe', () => {
    expect(needsConfirmation(look({ orientation: 'landscape' }))).toBe(true);
    expect(needsConfirmation(look({ orientation: 'portrait' }))).toBe(false);
  });
});

describe('blockedReason', () => {
  it('is null for a portrait look and a resolved voice', () => {
    expect(blockedReason(look(), voice(), false)).toBeNull();
  });

  it('holds a landscape look until the owner says they meant it', () => {
    const landscape = look({ orientation: 'landscape' });
    expect(blockedReason(landscape, voice(), false)).toMatch(/Confirm/);
    // A refusal would be wrong: the crop is a picture, not a failure, and a
    // head-and-shoulders look can crop to a usable 9:16. Choosing it without
    // knowing is the thing that must not happen.
    expect(blockedReason(landscape, voice(), true)).toBeNull();
  });

  it('waits for a voice the worker has not answered about', () => {
    expect(blockedReason(look(), voice({ status: 'pending' }), false)).toMatch(/Waiting/);
  });

  it('repeats what HeyGen said about a voice it does not recognise', () => {
    const rejected = voice({ status: 'unknown', error: 'voice_not_found' });
    expect(blockedReason(look(), rejected, false)).toBe('voice_not_found');
  });

  it('names the missing half rather than being mutely dead', () => {
    expect(blockedReason(null, voice(), false)).toMatch(/look/);
    expect(blockedReason(look(), null, false)).toMatch(/voice/);
  });
});

describe('toChoice', () => {
  it('carries the names, because a record of two hex ids answers nothing', () => {
    expect(toChoice(look(), voice())).toEqual({
      avatar_id: 'e6e4d0f4b4704568b32e8de751179c83',
      avatar_name: 'Dara',
      orientation: 'portrait',
      voice_id: '506420c8af914cb6a3cc3c350ccb411d',
      voice_name: 'Nadia (calm)',
    });
  });

  it('omits the engine rather than writing the default into it', () => {
    // A literal `avatar_iv` in the preset changes nothing at render time while
    // reading, in the row, like a decision someone made.
    expect(toChoice(look(), voice())).not.toHaveProperty('engine');
    expect(toChoice(look({ engines: ['avatar_iii'] }), voice())).toHaveProperty('engine', 'avatar_iii');
  });
});

describe('byUsability', () => {
  it('puts the looks that need no warning first', () => {
    const order = [
      look({ avatar_id: 'c', orientation: 'landscape', name: 'C' }),
      look({ avatar_id: 'a', orientation: 'portrait', name: 'A' }),
      look({ avatar_id: 'b', orientation: 'square', name: 'B' }),
      look({ avatar_id: 'd', orientation: 'unknown', name: 'D' }),
    ]
      .sort(byUsability)
      .map((row) => row.avatar_id);
    expect(order).toEqual(['a', 'b', 'd', 'c']);
  });
});
