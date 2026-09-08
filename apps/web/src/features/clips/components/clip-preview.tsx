/**
 * A scrub preview of the recording at a candidate's start point.
 *
 * This is what makes the clip gate reviewable without rendering anything, and
 * therefore what the whole feature rests on: the owner sees the moment in the
 * original file rather than reading two numbers and guessing. Rendering ten
 * candidates to let someone judge them would cost ten renders to discard nine.
 *
 * `preload="metadata"` so opening a gate with six candidates does not pull six
 * copies of a gigabyte recording. The browser fetches enough to seek and stops;
 * pressing play is what asks for the bytes.
 */

import { useQuery } from '@tanstack/react-query';
import { AlertCircleIcon } from 'lucide-react';
import { useEffect, useRef } from 'react';
import { Skeleton } from '@/components/ui/skeleton';
import { clipPreviewUrlQueryOptions } from '@/features/clips/api';

export function ClipPreview({ storageKey, start, end }: { storageKey: string; start: number; end: number }) {
  const { data: url, isPending, error } = useQuery(clipPreviewUrlQueryOptions(storageKey));
  const video = useRef<HTMLVideoElement>(null);

  // Seek once the file is seekable, not on mount: setting `currentTime` before
  // metadata has loaded is silently ignored, which would leave every preview
  // showing the first frame of the recording and every candidate looking
  // identical.
  useEffect(() => {
    const el = video.current;
    if (!el || !url) return;

    const seek = () => {
      // Clamped, because `duration` is the file's and `start` is the model's.
      // Seeking past the end leaves the element in an error state rather than
      // at the last frame.
      if (Number.isFinite(el.duration)) {
        el.currentTime = Math.min(start, Math.max(0, el.duration - 0.1));
      }
    };

    if (el.readyState >= 1) seek();
    el.addEventListener('loadedmetadata', seek);
    return () => el.removeEventListener('loadedmetadata', seek);
  }, [url, start]);

  // Stop at the end of the candidate's range rather than playing on into the
  // rest of the recording. Without this, reviewing a 12-second clip means
  // watching whatever follows it until you notice.
  useEffect(() => {
    const el = video.current;
    if (!el) return;

    const stopAtEnd = () => {
      if (el.currentTime >= end) el.pause();
    };
    el.addEventListener('timeupdate', stopAtEnd);
    return () => el.removeEventListener('timeupdate', stopAtEnd);
  }, [end]);

  if (isPending) return <Skeleton className="aspect-video w-full rounded-md" />;

  if (error || !url) {
    return (
      <div className="flex aspect-video w-full items-center justify-center gap-2 rounded-md border border-dashed bg-muted/30 p-4 text-center text-xs text-muted-foreground">
        <AlertCircleIcon className="size-4 shrink-0" aria-hidden />
        <span>Could not load the recording to preview. The timecodes below are still what will be cut.</span>
      </div>
    );
  }

  return (
    // biome-ignore lint/a11y/useMediaCaption: this is the raw recording, not a deliverable; its words are shown as text on the card beneath it
    <video
      ref={video}
      src={url}
      controls
      preload="metadata"
      playsInline
      className="aspect-video w-full rounded-md bg-black"
    />
  );
}
