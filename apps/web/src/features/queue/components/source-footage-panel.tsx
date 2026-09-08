import { CheckIcon, CircleIcon, LockIcon, SaveIcon, ShieldCheckIcon, TrashIcon, UploadIcon } from 'lucide-react';
import type { ReactNode } from 'react';
import { useRef, useState } from 'react';
import { toast } from 'sonner';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Spinner } from '@/components/ui/spinner';
import { Textarea } from '@/components/ui/textarea';
import { useOwner } from '@/features/auth/use-owner';
import {
  MAX_CONSENT_NOTE_CHARS,
  MAX_INSTRUCTION_CHARS,
  MIN_CONSENT_NOTE_CHARS,
  type ProductionRow,
} from '@/lib/database.types';
import { formatRelative } from '@/lib/format';
import {
  SOURCE_VIDEO_TYPES,
  useAttachSourceVideo,
  useClearSourceVideo,
  useConfirmSourceConsent,
  useSaveRenderInstruction,
} from '../api';
import { isScriptEditable, scriptLockReason } from '../pipeline-steps';
import { useSourceLane } from '../use-source-lane';

/**
 * The footage lane's three inputs, on the same screen as the script gate.
 *
 * This is where "here is a video, make something from it" is actually said. The
 * three things below are not a form in three parts — each is a separate write,
 * and each of them clears `script_approved_at`:
 *
 *   1. **The footage.** Uploaded straight to the private renders bucket. There
 *      is no server tier to stream a 200 MB file through, so the bytes go to
 *      Storage under a policy that admits owners writing under `sources/` and
 *      nothing else, and a second call records the object on the production.
 *   2. **The instruction.** This is the model's prompt, verbatim. That is why it
 *      is saved here and *approved* by the script gate rather than having an
 *      approve button of its own: on this lane the words a person signed off are
 *      what the model was told to do.
 *   3. **Consent.** A note, not a tick, because it is the field a rights
 *      question is answered out of a year later. It is cleared whenever the
 *      footage changes, since consent is about a particular file.
 *
 * Everything here is refused independently by Postgres for a viewer, for a
 * production whose render has been submitted, and — at approval — for any of the
 * three being absent. The states below mirror those guards so the reason is
 * visible rather than arriving as a failed request.
 */

/** Below this an instruction is a fragment rather than a direction. A warning, never a block. */
const SHORT_INSTRUCTION_CHARS = 20;

/**
 * The panel, but only where it belongs.
 *
 * Every screen that draws the script gate draws this too, and neither of them
 * should have to know which styles read footage -- so the question is asked
 * once, here. Renders nothing at all for the four lanes that generate from
 * text, which is every lane but one.
 */
export function SourceFootageSection({ production }: { production: ProductionRow }) {
  const { needsSource } = useSourceLane(production);
  if (!needsSource) return null;
  return <SourceFootagePanel production={production} />;
}

export function SourceFootagePanel({ production }: { production: ProductionRow }) {
  const { isOwner, isLoading: isRoleLoading } = useOwner();

  const editable = isScriptEditable(production);
  const lockReason = scriptLockReason(production);

  const attach = useAttachSourceVideo();
  const clear = useClearSourceVideo();
  const saveInstruction = useSaveRenderInstruction();
  const consent = useConfirmSourceConsent();
  const busy = attach.isPending || clear.isPending || saveInstruction.isPending || consent.isPending;

  const canWrite = isOwner && editable && !busy;

  const hasFootage = production.source_video_key !== null;
  const hasInstruction = (production.render_instruction ?? '').trim() !== '';
  const hasConsent = production.source_consent_at !== null;

  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap items-center gap-2">
          <CardTitle className="text-base">Your footage</CardTitle>
          {hasFootage && hasInstruction && hasConsent ? (
            <Badge variant="secondary">Ready</Badge>
          ) : (
            <Badge variant="outline">Incomplete</Badge>
          )}
        </div>
        <CardDescription>
          {editable
            ? 'This style makes a new reel out of a video you upload. All three below are needed before the script can be approved.'
            : 'What this video was made from.'}
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-6">
        <ol className="space-y-1 text-sm">
          <Checklist done={hasFootage}>A video to work from</Checklist>
          <Checklist done={hasInstruction}>An instruction saying what to do with it</Checklist>
          <Checklist done={hasConsent}>Consent for the people in it</Checklist>
        </ol>

        <FootageField production={production} canWrite={canWrite} attach={attach} clear={clear} />

        <InstructionField production={production} canWrite={canWrite} save={saveInstruction} />

        <ConsentField production={production} canWrite={canWrite} confirm={consent} />

        {!editable && lockReason && (
          <Alert>
            <LockIcon className="size-4" />
            <AlertTitle>Locked</AlertTitle>
            <AlertDescription>{lockReason}</AlertDescription>
          </Alert>
        )}

        {!isOwner && !isRoleLoading && (
          <Alert>
            <AlertTitle>Read-only</AlertTitle>
            <AlertDescription>
              Your account is a viewer, so you can see what was uploaded but not change it. An owner can promote you.
            </AlertDescription>
          </Alert>
        )}
      </CardContent>
    </Card>
  );
}

function Checklist({ done, children }: { done: boolean; children: ReactNode }) {
  return (
    <li className="flex items-center gap-2">
      {done ? (
        <CheckIcon className="size-4 shrink-0 text-muted-foreground" aria-hidden />
      ) : (
        <CircleIcon className="size-4 shrink-0 text-muted-foreground" aria-hidden />
      )}
      <span className={done ? 'text-muted-foreground line-through' : undefined}>{children}</span>
      <span className="sr-only">{done ? '(done)' : '(still needed)'}</span>
    </li>
  );
}

// ---------------------------------------------------------------------------
// 1. The footage
// ---------------------------------------------------------------------------

type Attach = ReturnType<typeof useAttachSourceVideo>;
type Clear = ReturnType<typeof useClearSourceVideo>;

function FootageField({
  production,
  canWrite,
  attach,
  clear,
}: {
  production: ProductionRow;
  canWrite: boolean;
  attach: Attach;
  clear: Clear;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [chosen, setChosen] = useState<File | null>(null);

  const runUpload = async () => {
    if (!chosen) return;
    try {
      await attach.mutateAsync({ productionId: production.id, file: chosen });
      setChosen(null);
      if (input.current) input.current.value = '';
      toast.success('Footage attached', {
        description: 'Any earlier approval and consent were cleared, because both were about a different file.',
      });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Could not upload that video');
    }
  };

  const runClear = async () => {
    try {
      await clear.mutateAsync({ productionId: production.id });
      toast.success('Footage removed');
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Could not remove the footage');
    }
  };

  return (
    <div className="space-y-2">
      <Label htmlFor="source-video">The video</Label>

      {production.source_video_key !== null && (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border p-3 text-sm">
          <div>
            <p className="font-medium">{production.source_video_name ?? 'Uploaded video'}</p>
            <p className="text-muted-foreground">
              {formatBytes(production.source_video_bytes)} · attached{' '}
              {formatRelative(production.source_video_uploaded_at)}
            </p>
          </div>
          <Button variant="ghost" size="sm" onClick={() => void runClear()} disabled={!canWrite}>
            {clear.isPending ? <Spinner className="size-4" /> : <TrashIcon className="size-4" />}
            Remove
          </Button>
        </div>
      )}

      <div className="flex flex-col gap-2 sm:flex-row">
        <Input
          id="source-video"
          ref={input}
          type="file"
          accept={SOURCE_VIDEO_TYPES.join(',')}
          disabled={!canWrite}
          onChange={(event) => setChosen(event.target.files?.[0] ?? null)}
        />
        <Button onClick={() => void runUpload()} disabled={!canWrite || chosen === null}>
          {attach.isPending ? <Spinner className="size-4" /> : <UploadIcon className="size-4" />}
          {production.source_video_key === null ? 'Upload' : 'Replace'}
        </Button>
      </div>

      <p className="text-sm text-muted-foreground">
        MP4, QuickTime .mov or WebM, up to 500 MB. Uploading a different file clears your approval and your consent
        record, because both were about the file it replaces.
      </p>
    </div>
  );
}

/** Bytes as the upload reported them, in the unit a person reads. */
export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return 'size unknown';
  if (bytes < 1024) return `${bytes} B`;
  const mb = bytes / 1024 / 1024;
  if (mb < 1) return `${Math.round(bytes / 1024)} KB`;
  if (mb < 1024) return `${mb < 10 ? mb.toFixed(1) : Math.round(mb)} MB`;
  return `${(mb / 1024).toFixed(1)} GB`;
}

// ---------------------------------------------------------------------------
// 2. The instruction
// ---------------------------------------------------------------------------

export interface InstructionValidity {
  ok: boolean;
  error: string | null;
  warning: string | null;
}

/**
 * The same rules `clean_render_instruction` enforces in Postgres, for feedback only.
 *
 * Exported because it is the part of this file worth testing directly: the
 * boundaries are exact numbers that have to agree with a check constraint, and
 * agreeing with it is not something you can see by looking at the page.
 */
export function validateInstruction(instruction: string): InstructionValidity {
  const trimmed = instruction.trim();
  if (!trimmed) {
    return { ok: false, error: 'An instruction cannot be empty: it is what the model is told to do.', warning: null };
  }
  if (trimmed.length > MAX_INSTRUCTION_CHARS) {
    return {
      ok: false,
      error: `That is ${trimmed.length.toLocaleString()} characters. The limit is ${MAX_INSTRUCTION_CHARS.toLocaleString()}, which is what the database enforces.`,
      warning: null,
    };
  }
  if (trimmed.length < SHORT_INSTRUCTION_CHARS) {
    return {
      ok: true,
      error: null,
      warning: 'That is very short. The model has only these words and your footage to go on.',
    };
  }
  return { ok: true, error: null, warning: null };
}

type SaveInstruction = ReturnType<typeof useSaveRenderInstruction>;

function InstructionField({
  production,
  canWrite,
  save,
}: {
  production: ProductionRow;
  canWrite: boolean;
  save: SaveInstruction;
}) {
  const server = production.render_instruction ?? '';

  // Seeded from the row and re-seeded only when the row's own text changes,
  // the same way the script editor does it: the pipeline never writes this
  // column, but `attach_source_video` and a Realtime update both re-render
  // this component, and neither should discard what somebody is typing.
  const [seed, setSeed] = useState(server);
  const [draft, setDraft] = useState(server);
  if (server !== seed) {
    const untouched = draft === seed;
    setSeed(server);
    if (untouched) setDraft(server);
  }

  const validity = validateInstruction(draft);
  const isDirty = draft !== server;

  const run = async () => {
    try {
      await save.mutateAsync({ productionId: production.id, instruction: draft });
      toast.success('Instruction saved', {
        description: 'Nothing renders until you approve the script and this together.',
      });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Could not save the instruction');
    }
  };

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <Label htmlFor="render-instruction">What to do with it</Label>
        <span className={validity.error ? 'text-xs text-destructive' : 'text-xs text-muted-foreground'}>
          {draft.trim().length.toLocaleString()} / {MAX_INSTRUCTION_CHARS.toLocaleString()} characters
        </span>
      </div>
      <Textarea
        id="render-instruction"
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        placeholder="Cut this to a 30-second vertical reel, keep the two clearest sentences, and grade it warm."
        rows={4}
        disabled={!canWrite}
        aria-invalid={validity.error !== null}
        aria-describedby="instruction-validity"
      />
      <p id="instruction-validity" className="text-sm">
        {validity.error ? (
          <span className="text-destructive">{validity.error}</span>
        ) : validity.warning ? (
          <span className="text-muted-foreground">{validity.warning}</span>
        ) : (
          <span className="text-muted-foreground">
            This is sent to the model word for word, which is why you approve it alongside the script.
          </span>
        )}
      </p>
      <Button variant="outline" onClick={() => void run()} disabled={!canWrite || !validity.ok || !isDirty}>
        {save.isPending ? <Spinner className="size-4" /> : <SaveIcon className="size-4" />}
        Save the instruction
      </Button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 3. Consent
// ---------------------------------------------------------------------------

type Confirm = ReturnType<typeof useConfirmSourceConsent>;

function ConsentField({
  production,
  canWrite,
  confirm,
}: {
  production: ProductionRow;
  canWrite: boolean;
  confirm: Confirm;
}) {
  const [note, setNote] = useState('');
  const trimmed = note.trim();
  const longEnough = trimmed.length >= MIN_CONSENT_NOTE_CHARS && trimmed.length <= MAX_CONSENT_NOTE_CHARS;

  const run = async () => {
    try {
      await confirm.mutateAsync({ productionId: production.id, note });
      setNote('');
      toast.success('Consent recorded');
    } catch (error) {
      toast.error(error instanceof Error ? error.message : 'Could not record consent');
    }
  };

  if (production.source_consent_at !== null) {
    return (
      <div className="space-y-2">
        <Label>Consent</Label>
        <Alert>
          <ShieldCheckIcon className="size-4" />
          <AlertTitle>Recorded {formatRelative(production.source_consent_at)}</AlertTitle>
          <AlertDescription>
            <p className="whitespace-pre-wrap">{production.source_consent_note}</p>
          </AlertDescription>
        </Alert>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      <Label htmlFor="consent-note">Consent</Label>
      <p className="text-sm text-muted-foreground">
        Say who appears in the footage and how they agreed to it being used this way. This is the record the system
        keeps, so write it for someone reading it in a year — not for the button below.
      </p>
      <Textarea
        id="consent-note"
        value={note}
        onChange={(event) => setNote(event.target.value)}
        placeholder="Filmed at our own office. Everyone on camera is a member of staff and agreed in writing to it being used in social posts."
        rows={3}
        disabled={!canWrite || production.source_video_key === null}
        aria-describedby="consent-hint"
      />
      <p id="consent-hint" className="text-sm text-muted-foreground">
        {production.source_video_key === null
          ? 'Upload the footage first — consent is recorded against a particular file.'
          : `At least ${MIN_CONSENT_NOTE_CHARS} characters. Replacing the footage clears this.`}
      </p>
      <Button
        variant="outline"
        onClick={() => void run()}
        disabled={!canWrite || production.source_video_key === null || !longEnough}
      >
        {confirm.isPending ? <Spinner className="size-4" /> : <ShieldCheckIcon className="size-4" />}
        Record consent
      </Button>
    </div>
  );
}
