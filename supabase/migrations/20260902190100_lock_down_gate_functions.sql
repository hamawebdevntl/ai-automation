-- SECURITY FIX for 20260902190000.
--
-- That migration used `revoke all on function ... from public`, intending to
-- make the gate-token functions callable only by the service role. It does not
-- work. `public` is the SQL pseudo-role; Supabase separately grants EXECUTE on
-- new functions in the `public` schema to the `anon` and `authenticated` roles
-- through default privileges, and revoking from the pseudo-role leaves those
-- grants untouched.
--
-- Verified against the live project before this fix: an anon-key caller got
-- HTTP 204 from `set_gate_token` and HTTP 200 from `take_gate_token` --
-- i.e. the browser could read a Step Functions callback token. That is exactly
-- what `productions.execution_arn`'s comment forbids, and it would let anyone
-- holding the publishable key claim a token and block a legitimate resume.
--
-- The roles must be named explicitly.

revoke all on function public.set_gate_token(uuid, text)          from public, anon, authenticated;
revoke all on function public.take_gate_token(uuid)               from public, anon, authenticated;
revoke all on function public.release_gate_token(uuid)            from public, anon, authenticated;
revoke all on function public.record_gate_decision(uuid, text)    from public, anon, authenticated;
revoke all on function public.peek_gate_decision(uuid)            from public, anon, authenticated;
revoke all on function public.list_pending_gate_resumes()         from public, anon, authenticated;

grant execute on function public.set_gate_token(uuid, text)       to service_role;
grant execute on function public.take_gate_token(uuid)            to service_role;
grant execute on function public.release_gate_token(uuid)         to service_role;
grant execute on function public.record_gate_decision(uuid, text) to service_role;
grant execute on function public.peek_gate_decision(uuid)         to service_role;
grant execute on function public.list_pending_gate_resumes()      to service_role;

-- Clear the token and decision rows written during the pre-fix verification.
delete from private.gate_tokens;
delete from private.gate_decisions;
