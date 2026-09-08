import type { HeyGenLookRow, HeyGenVoiceRow, LookOrientation, PresenterChoice } from '@/lib/database.types';

/**
 * What the picker knows about a look that the picker's markup should not have
 * to work out.
 *
 * Both rules here are already in the lane's own code and in `presenter_choice`
 * in Postgres. They are restated as pure functions because the interesting
 * failure is not a bad save — Postgres refuses those — but a *silent* one: a
 * landscape look saved without anyone noticing, and a reel that crops the
 * speaker's head off arriving at Gate 2 as the first hint.
 */

/** Omitting `engine` at submit time selects Avatar IV, so this is the engine
 *  that runs when nothing names one. */
export const DEFAULT_ENGINE = 'avatar_iv';

/**
 * The engine to send for a look, or null to let HeyGen pick.
 *
 * Null when the look advertises Avatar IV or advertises nothing at all: in
 * both cases the default is right, and writing the value out anyway would put
 * a decision in the preset that nobody made. Otherwise the first engine it
 * does advertise, which is what makes a studio avatar from HeyGen's public
 * catalogue renderable rather than a terminal failure.
 */
export function engineFor(look: HeyGenLookRow): string | null {
  if (look.engines.length === 0) return null;
  if (look.engines.includes(DEFAULT_ENGINE)) return null;
  return look.engines[0] ?? null;
}

/** Whether a look can be rendered at all — false only for one advertising an
 *  empty-but-present engine list, which no response should produce. */
export function isRenderable(look: HeyGenLookRow): boolean {
  return look.engines.length === 0 || engineFor(look) !== null || look.engines.includes(DEFAULT_ENGINE);
}

const ORIENTATION_WARNINGS: Partial<Record<LookOrientation, string>> = {
  landscape:
    'This look is landscape. A 9:16 reel from it crops the speaker to fill the frame, usually through the top of the head.',
  square:
    'This look is square. A 9:16 reel from it crops the sides, which is milder than landscape but still not the framing it was shot for.',
  unknown: 'HeyGen did not say how this look is framed, so whether a 9:16 reel crops it cannot be told from here.',
};

/**
 * The sentence to show beside a look, or null when there is nothing to say.
 *
 * A warning rather than a refusal: a landscape look with `fit: cover` is a
 * choice someone may want, and the lane supports it. What is not acceptable is
 * making it silently.
 */
export function orientationWarning(look: HeyGenLookRow): string | null {
  return ORIENTATION_WARNINGS[look.orientation] ?? null;
}

/** Whether picking this look should require the owner to say they meant it. */
export function needsConfirmation(look: HeyGenLookRow): boolean {
  return look.orientation === 'landscape';
}

/** How a voice reads while the worker has not answered about it yet. */
export function voiceLabel(voice: HeyGenVoiceRow | null | undefined): string {
  if (!voice) return 'Not chosen';
  if (voice.status === 'pending') return `Checking ${voice.voice_id} with HeyGen…`;
  if (voice.status === 'unknown') return `HeyGen does not recognise ${voice.voice_id}`;
  return voice.name ?? voice.voice_id;
}

/** A voice that can actually be saved. */
export function isUsable(voice: HeyGenVoiceRow | null | undefined): voice is HeyGenVoiceRow {
  return voice?.status === 'ok';
}

/**
 * The pair as it will be stored, names included.
 *
 * The names are carried because the ids are hex and the look they came from
 * may since have been deleted from the account: a record naming
 * `e6e4d0f4…` and nothing else cannot answer "who presented this reel?".
 */
export function toChoice(look: HeyGenLookRow, voice: HeyGenVoiceRow): PresenterChoice {
  const engine = engineFor(look);
  return {
    avatar_id: look.avatar_id,
    avatar_name: look.name,
    orientation: look.orientation,
    voice_id: voice.voice_id,
    voice_name: voice.name,
    ...(engine ? { engine } : {}),
  };
}

/**
 * The reason this pair cannot be saved, or null when it can.
 *
 * Postgres refuses the same cases, and it is the refusal that actually holds.
 * This exists so the button says why it is disabled rather than being mutely
 * dead — the same split as the trend settings' bounds.
 */
export function blockedReason(
  look: HeyGenLookRow | null,
  voice: HeyGenVoiceRow | null,
  confirmed: boolean,
): string | null {
  if (!look) return 'Pick a look.';
  if (!isRenderable(look)) return 'This look advertises no engine we can render it on.';
  if (!voice) return 'Add a voice id.';
  if (voice.status === 'pending') return 'Waiting for HeyGen to confirm that voice.';
  if (voice.status === 'unknown') return voice.error ?? 'HeyGen does not recognise that voice id.';
  if (needsConfirmation(look) && !confirmed) return 'Confirm you want a landscape look first.';
  return null;
}

/** Portrait first, then square, then the ones that crop or cannot be told —
 *  the order an owner should be reading them in. */
const ORDER: Record<LookOrientation, number> = { portrait: 0, square: 1, unknown: 2, landscape: 3 };

export function byUsability(a: HeyGenLookRow, b: HeyGenLookRow): number {
  const rank = ORDER[a.orientation] - ORDER[b.orientation];
  return rank !== 0 ? rank : (a.name ?? a.avatar_id).localeCompare(b.name ?? b.avatar_id);
}
