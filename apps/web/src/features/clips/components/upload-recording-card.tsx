/**
 * Upload a long recording to be clipped.
 *
 * Three inputs, and only one of them is a real decision. The file and the style
 * are required; the number of clips to look for is the one worth thinking about,
 * because it is a statement about how much reviewing the owner wants to do
 * rather than about the recording — `docs/GAPS.md` B7 already counts two gates
 * times ten reels a day as twenty unmodelled review actions, and this gate adds
 * to that queue.
 *
 * The style is chosen once here rather than per candidate on purpose: the gate
 * is meant to be a fast yes/no over several clips, and a style picker on each
 * one would turn it into several Gate 1 decisions.
 */

import { useQuery } from '@tanstack/react-query';
import { Loader2Icon, UploadIcon } from 'lucide-react';
import { useRef, useState } from 'react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { NativeSelect } from '@/components/ui/native-select';
import {
  CLIP_SOURCE_MAX_BYTES,
  CLIP_SOURCE_TYPES,
  clipSourceFileError,
  useUploadClipSource,
} from '@/features/clips/api';
import { stylePresetsQueryOptions } from '@/features/queue/api';
import { DEFAULT_CANDIDATE_CAP, MAX_CANDIDATE_CAP, MIN_CANDIDATE_CAP } from '@/lib/database.types';
import { formatCostRange } from '@/lib/format';
import { toError } from '@/lib/supabase-error';

/** Only presets that actually cut a clip. Any other style would ignore the range. */
const CLIP_RENDER_MODE = 'clip';

export function UploadRecordingCard({ canUpload }: { canUpload: boolean }) {
  const { data: presets, isPending: presetsPending, error: presetsError } = useQuery(stylePresetsQueryOptions());
  const upload = useUploadClipSource();

  const fileInput = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [styleId, setStyleId] = useState('');
  const [cap, setCap] = useState(DEFAULT_CANDIDATE_CAP);

  const clipStyles = (presets ?? []).filter((preset) => preset.render_mode === CLIP_RENDER_MODE);
  const chosen = styleId || clipStyles[0]?.id || '';
  const fileProblem = file ? clipSourceFileError(file) : null;

  // A failed preset read must not leave the button enabled. `isPending` alone
  // would: an error resolves the query, so a network blip would offer an upload
  // with no style to attach it to, which `create_clip_source` then refuses.
  const stylesUnknown = presetsPending || Boolean(presetsError);
  const blocked = !canUpload || stylesUnknown || !chosen || !file || Boolean(fileProblem);

  const submit = () => {
    if (!file || !chosen) return;
    upload.mutate(
      { file, styleId: chosen, candidateCap: cap },
      {
        onSuccess: () => {
          setFile(null);
          if (fileInput.current) fileInput.current.value = '';
          toast.success('Recording uploaded', {
            description: 'Transcribing it now. The clips it finds will appear below.',
          });
        },
        onError: (error) => toast.error('Could not upload that recording', { description: toError(error).message }),
      },
    );
  };

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">Clip a recording</CardTitle>
        <CardDescription>
          Upload a talk, a webinar or a call. It is transcribed once, a model proposes the moments worth cutting, and
          you pick which ones get made. Nothing renders until you do.
        </CardDescription>
      </CardHeader>

      <CardContent className="space-y-4">
        <div className="space-y-1.5">
          <Label htmlFor="clip-source-file">Recording</Label>
          <Input
            id="clip-source-file"
            ref={fileInput}
            type="file"
            accept={CLIP_SOURCE_TYPES.join(',')}
            disabled={!canUpload || upload.isPending}
            onChange={(event) => setFile(event.target.files?.[0] ?? null)}
          />
          <p className="text-xs text-muted-foreground">
            MP4, QuickTime .mov or WebM, up to {CLIP_SOURCE_MAX_BYTES / 1024 / 1024 / 1024} GB.
          </p>
          {fileProblem && <p className="text-xs text-destructive">{fileProblem}</p>}
        </div>

        <div className="grid gap-4 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor="clip-source-style">Style for the clips</Label>
            <NativeSelect
              id="clip-source-style"
              value={chosen}
              disabled={!canUpload || stylesUnknown || upload.isPending}
              onChange={(event) => setStyleId(event.target.value)}
            >
              {clipStyles.map((preset) => (
                <option key={preset.id} value={preset.id}>
                  {preset.name} · {formatCostRange(preset)}
                </option>
              ))}
            </NativeSelect>
            {!stylesUnknown && clipStyles.length === 0 && (
              <p className="text-xs text-destructive">
                No active style cuts clips. The `clip-cut` preset must be active before a recording can be uploaded.
              </p>
            )}
            {presetsError && (
              <p className="text-xs text-destructive">Could not load the styles: {toError(presetsError).message}</p>
            )}
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="clip-source-cap">Clips to look for</Label>
            <Input
              id="clip-source-cap"
              type="number"
              min={MIN_CANDIDATE_CAP}
              max={MAX_CANDIDATE_CAP}
              value={cap}
              disabled={!canUpload || upload.isPending}
              onChange={(event) =>
                setCap(
                  Math.max(
                    MIN_CANDIDATE_CAP,
                    Math.min(MAX_CANDIDATE_CAP, Number(event.target.value) || DEFAULT_CANDIDATE_CAP),
                  ),
                )
              }
            />
            <p className="text-xs text-muted-foreground">
              At most this many, and fewer if the recording does not have that many good moments. Every one is a review
              action, so ask for what you will actually watch.
            </p>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <Button disabled={blocked || upload.isPending} onClick={submit}>
            {upload.isPending ? (
              <Loader2Icon className="size-4 animate-spin" aria-hidden />
            ) : (
              <UploadIcon className="size-4" aria-hidden />
            )}
            {upload.isPending ? 'Uploading…' : 'Upload and find clips'}
          </Button>
          {!canUpload && <span className="text-xs text-muted-foreground">Only an owner can upload a recording.</span>}
          {upload.isPending && (
            <span className="text-xs text-muted-foreground">
              A long recording takes a while to transfer. Leaving this page cancels it.
            </span>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
