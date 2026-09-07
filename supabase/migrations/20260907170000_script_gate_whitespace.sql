-- =============================================================================
-- A newline is not a script
-- =============================================================================
--
-- `btrim(text)` with one argument strips **spaces**. Only spaces. Tabs,
-- newlines and carriage returns survive it, so `btrim(E'  \n  ')` is `E'\n'` --
-- which is not the empty string, which meant `clean_script` accepted it and
-- `productions_approved_script_not_empty` was satisfied by it.
--
-- The effect was a script gate that could be passed by pressing Enter. Caught
-- by driving a real production through the gate against this database rather
-- than by reading the SQL, which is the only way it was ever going to be
-- caught: every unit test on the browser's side uses JavaScript's `trim()`,
-- and that one does handle all whitespace. The two implementations of "is this
-- empty" disagreed exactly where it mattered.
--
-- Both places now name the characters explicitly. `E' \t\n\r\f\v'` is the same
-- set JavaScript's `trim()` treats as whitespace for the purposes of anything
-- an LLM or a person will ever type into this box.

-- -----------------------------------------------------------------------------
-- The constraint
-- -----------------------------------------------------------------------------
alter table public.productions drop constraint if exists productions_approved_script_not_empty;
alter table public.productions add constraint productions_approved_script_not_empty
  check (
    script_approved_at is null
    or btrim(coalesce(script, ''), E' \t\n\r\f\v') <> ''
  );

-- -----------------------------------------------------------------------------
-- The validator
-- -----------------------------------------------------------------------------
--
-- Also now returns the text trimmed of all of it, so a script stored through
-- this function never carries leading or trailing blank lines into a TTS pass.
create or replace function public.clean_script(p_script text)
returns text
language plpgsql
immutable
set search_path = public
as $$
declare
  v text := btrim(coalesce(p_script, ''), E' \t\n\r\f\v');
begin
  if v = '' then
    raise exception 'A script cannot be empty' using errcode = '22023';
  end if;
  if length(v) > 5000 then
    raise exception 'That script is % characters; the limit is 5000, which is what HeyGen accepts', length(v)
      using errcode = '22023';
  end if;
  return v;
end;
$$;

revoke all on function public.clean_script(text) from public, anon, authenticated;
