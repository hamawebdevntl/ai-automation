import { UploadIcon } from 'lucide-react';
import { useId, useRef, useState } from 'react';
import { toast } from 'sonner';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group';
import { Spinner } from '@/components/ui/spinner';
import { Textarea } from '@/components/ui/textarea';
import { useOwner } from '@/features/auth/use-owner';
import { toError } from '@/lib/supabase-error';
import { useUploadFinishedCut } from '../api';
import {
  formatBytes,
  MAX_BRIEF_CHARS,
  MAX_TITLE_CHARS,
  MAX_UPLOAD_BYTES,
  UPLOAD_CONTENT_TYPE,
  type UploadDraft,
  validateUpload,
} from '../upload';

/**
 * Putting a video that already exists into the publishing half of the pipeline.
 *
 * It lives on the Gate 2 page rather than on the queue because that is where it
 * arrives: an upload has no idea to approve and no script to sign off, so the
 * first — and only — decision anyone makes about it is the one this page is
 * for.
 *
 * Two of the three fields are here because a generated production gets them
 * from steps an upload does not have. The title stands in for the idea, the
 * description stands in for the approved script, and both are what the
 * per-platform copy is written from. The third, the AI disclosure, is here
 * because nothing else in the system can answer it: everything the pipeline
 * renders is AI-generated and says so, and an uploaded file might be anything.
 *
 * Problems are shown only after a submit is attempted. Marking a form invalid
 * before anyone has finished filling it in is noise, and this one is short
 * enough that a single pass at the end is not a hunt.
 */
export function UploadCutPanel() {
  const { isOwner, isLoading: isRoleLoading } = useOwner();
  const upload = useUploadFinishedCut();
  const fileInput = useRef<HTMLInputElement>(null);
  const fieldId = useId();

  const [draft, setDraft] = useState<UploadDraft>({ file: null, title: '', brief: '', isAigc: null });
  const [attempted, setAttempted] = useState(false);

  const problems = validateUpload(draft);
  const shown = attempted ? problems : {};
  const busy = upload.isPending;

  function patch(change: Partial<UploadDraft>) {
    setDraft((previous) => ({ ...previous, ...change }));
  }

  function reset() {
    setDraft({ file: null, title: '', brief: '', isAigc: null });
    setAttempted(false);
    // The input holds the filename itself, so clearing state is not enough:
    // choosing the same file again would fire no change event.
    if (fileInput.current) fileInput.current.value = '';
  }

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setAttempted(true);
    if (Object.keys(problems).length > 0 || !draft.file || draft.isAigc === null) return;

    try {
      await upload.mutateAsync({
        file: draft.file,
        title: draft.title,
        brief: draft.brief,
        isAigc: draft.isAigc,
      });
      toast.success('Uploaded', {
        description: 'The quality check runs on it next, then it comes back here for your sign-off.',
      });
      reset();
    } catch (error) {
      toast.error('Could not upload that cut', { description: toError(error).message });
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Upload a finished cut</CardTitle>
        <CardDescription>
          For a video made outside the pipeline. It skips idea approval, the script gate and the render, and comes
          straight here for sign-off — with the same quality check and the same per-platform copy as everything else.
        </CardDescription>
      </CardHeader>

      <CardContent>
        <form className="space-y-4" onSubmit={submit}>
          <div className="space-y-2">
            <Label htmlFor={`${fieldId}-file`}>The cut</Label>
            <Input
              id={`${fieldId}-file`}
              ref={fileInput}
              type="file"
              accept={UPLOAD_CONTENT_TYPE}
              disabled={!isOwner || busy}
              onChange={(event) => patch({ file: event.target.files?.[0] ?? null })}
              aria-invalid={shown.file !== undefined}
              aria-describedby={`${fieldId}-file-hint`}
            />
            <p id={`${fieldId}-file-hint`} className="text-xs text-muted-foreground">
              MP4, vertical 9:16, up to {formatBytes(MAX_UPLOAD_BYTES)}.
              {draft.file ? ` Selected: ${draft.file.name} (${formatBytes(draft.file.size)}).` : ''}
            </p>
            <FieldProblem message={shown.file} />
          </div>

          <div className="space-y-2">
            <Label htmlFor={`${fieldId}-title`}>Title</Label>
            <Input
              id={`${fieldId}-title`}
              value={draft.title}
              maxLength={MAX_TITLE_CHARS}
              disabled={!isOwner || busy}
              placeholder="What this video is called"
              onChange={(event) => patch({ title: event.target.value })}
              aria-invalid={shown.title !== undefined}
            />
            <FieldProblem message={shown.title} />
          </div>

          <div className="space-y-2">
            <Label htmlFor={`${fieldId}-brief`}>What it is about</Label>
            <Textarea
              id={`${fieldId}-brief`}
              value={draft.brief}
              rows={3}
              maxLength={MAX_BRIEF_CHARS}
              disabled={!isOwner || busy}
              placeholder="A couple of sentences. This is what the caption, the title and the description are written from."
              onChange={(event) => patch({ brief: event.target.value })}
              aria-invalid={shown.brief !== undefined}
            />
            <p className="text-xs text-muted-foreground">
              There is no script on an uploaded cut, so this is the only thing the copy has to go on.
            </p>
            <FieldProblem message={shown.brief} />
          </div>

          <fieldset className="space-y-2" disabled={!isOwner || busy}>
            <legend className="text-sm font-medium">Is this video AI-generated?</legend>
            <p className="text-xs text-muted-foreground">
              TikTok, YouTube and Meta all require the disclosure, and only you know the answer. Everything this
              pipeline renders is declared as AI-generated.
            </p>
            <RadioGroup
              className="flex gap-6"
              value={draft.isAigc === null ? '' : draft.isAigc ? 'yes' : 'no'}
              onValueChange={(value) => patch({ isAigc: value === 'yes' })}
            >
              <div className="flex items-center gap-2">
                <RadioGroupItem value="yes" id={`${fieldId}-aigc-yes`} />
                <Label htmlFor={`${fieldId}-aigc-yes`} className="font-normal">
                  Yes
                </Label>
              </div>
              <div className="flex items-center gap-2">
                <RadioGroupItem value="no" id={`${fieldId}-aigc-no`} />
                <Label htmlFor={`${fieldId}-aigc-no`} className="font-normal">
                  No
                </Label>
              </div>
            </RadioGroup>
            <FieldProblem message={shown.isAigc} />
          </fieldset>

          {!isOwner && !isRoleLoading && (
            <Alert>
              <AlertTitle>Read-only</AlertTitle>
              <AlertDescription>Your account is a viewer. An owner has to upload a cut.</AlertDescription>
            </Alert>
          )}

          <Alert>
            <AlertTitle>Music has to be in the file</AlertTitle>
            <AlertDescription>
              Instagram cannot attach licensed audio through the API, so whatever this video should sound like has to be
              part of the upload.
            </AlertDescription>
          </Alert>

          <Button type="submit" disabled={!isOwner || busy}>
            {busy ? <Spinner className="size-4" /> : <UploadIcon className="size-4" />}
            {busy ? 'Uploading…' : 'Upload and check'}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}

function FieldProblem({ message }: { message?: string }) {
  if (!message) return null;
  return <p className="text-sm text-destructive">{message}</p>;
}
