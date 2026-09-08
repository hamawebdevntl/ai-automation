import type { PresenterChoice, StylePresetRow } from '@/lib/database.types';

/**
 * The avatar and voice a preset currently names.
 *
 * `params` is `Json`, which is the honest type for a column nothing validates
 * column-side, so reading it needs a narrowing rather than a cast. The two ids
 * are the only keys this feature owns; everything else under `params.heygen`
 * is how the reel is cut and is left alone.
 */
export function presenterOf(preset: StylePresetRow | null | undefined): Partial<PresenterChoice> | null {
  if (!preset) return null;
  const params = preset.params;
  if (typeof params !== 'object' || params === null || Array.isArray(params)) return null;
  const heygen = (params as Record<string, unknown>).heygen;
  if (typeof heygen !== 'object' || heygen === null || Array.isArray(heygen)) return null;

  const record = heygen as Record<string, unknown>;
  const avatarId = typeof record.avatar_id === 'string' ? record.avatar_id : null;
  const voiceId = typeof record.voice_id === 'string' ? record.voice_id : null;
  const engine = typeof record.engine === 'string' ? record.engine : undefined;
  // Null rather than a half-empty object: the render step raises without an
  // avatar id, so a preset with only a voice names no presenter at all.
  if (!avatarId) return null;
  return { avatar_id: avatarId, voice_id: voiceId ?? undefined, ...(engine ? { engine } : {}) };
}
