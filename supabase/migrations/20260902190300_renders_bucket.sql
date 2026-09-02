-- The renders bucket.
--
-- Private on purpose. Postiz requires a public HTTPS URL to pull from, but
-- making the bucket itself public would expose every unreviewed cut to anyone
-- who guessed a path -- and the web app has open signup with read-everything
-- policies. The pipeline mints a short-lived signed URL at publish time
-- instead, which satisfies Postiz without publishing anything.
--
-- The 50MiB project default is too low: a 60-second 1080x1920 render at a high
-- bitrate exceeds it, and the failure would land mid-pipeline after the render
-- has already been paid for.

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
  'renders',
  'renders',
  false,
  524288000,  -- 500 MB
  array['video/mp4', 'image/jpeg']
)
on conflict (id) do update
  set public             = false,
      file_size_limit    = excluded.file_size_limit,
      allowed_mime_types = excluded.allowed_mime_types;

-- No storage RLS policies are added deliberately. The pipeline writes with the
-- service role, which bypasses row-level security, and the review UI reads
-- through signed URLs, which do not consult RLS at all. Any policy here would
-- be decoration.
