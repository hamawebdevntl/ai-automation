-- Cache the Postiz media reference on the production.
--
-- `POST /public/v1/upload-from-url` is not idempotent: every call re-fetches
-- the entire video and buffers it in Postiz's memory. Publishing fans out to
-- four platforms and may be re-driven, so the upload happens once and the
-- resulting id/path are reused for every post.

alter table public.productions add column if not exists postiz_media_id   text;
alter table public.productions add column if not exists postiz_media_path text;

comment on column public.productions.postiz_media_id is
  'Postiz media id from upload-from-url, reused across the platform fanout so the video is only transferred once.';
