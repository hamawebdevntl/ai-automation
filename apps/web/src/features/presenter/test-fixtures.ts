import type { HeyGenLookRow, HeyGenVoiceRow, StylePresetRow } from '@/lib/database.types';

/** A portrait look from this organisation's own avatar group — the shape the
 *  presenter lane shipped with, and the only shape that needs no warning. */
export function look(overrides: Partial<HeyGenLookRow> = {}): HeyGenLookRow {
  return {
    avatar_id: 'e6e4d0f4b4704568b32e8de751179c83',
    name: 'Dara',
    preview_image_url: 'https://files.heygen.ai/dara.jpg',
    preview_video_url: null,
    orientation: 'portrait',
    engines: ['avatar_iii', 'avatar_iv', 'avatar_v'],
    default_voice_id: null,
    gender: 'female',
    ownership: 'private',
    seen_at: '2026-09-08T09:00:00Z',
    ...overrides,
  };
}

export function voice(overrides: Partial<HeyGenVoiceRow> = {}): HeyGenVoiceRow {
  return {
    voice_id: '506420c8af914cb6a3cc3c350ccb411d',
    status: 'ok',
    name: 'Nadia (calm)',
    language: 'English',
    gender: 'female',
    preview_audio_url: 'https://files.heygen.ai/dara.mp3',
    error: null,
    requested_at: '2026-09-08T09:00:00Z',
    resolved_at: '2026-09-08T09:00:05Z',
    ...overrides,
  };
}

export function presenterPreset(overrides: Partial<StylePresetRow> = {}): StylePresetRow {
  return {
    id: 'preset-presenter',
    slug: 'ai-presenter',
    name: 'AI presenter',
    description: 'A talking-head avatar delivering the script to camera.',
    lane: 'presenter',
    video_source: 'heygen',
    render_mode: 'heygen',
    est_cost_min_usd: 1,
    est_cost_max_usd: 2,
    est_minutes: 18,
    params: {
      lane: 'heygen',
      heygen: {
        avatar_id: 'e6e4d0f4b4704568b32e8de751179c83',
        voice_id: '506420c8af914cb6a3cc3c350ccb411d',
        aspect_ratio: '9:16',
        resolution: '1080p',
        burn_captions: true,
      },
    },
    is_active: true,
    sort_order: 30,
    created_at: '2026-09-03T00:00:00Z',
    ...overrides,
  };
}
