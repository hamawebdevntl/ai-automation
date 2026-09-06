/**
 * Making a supabase-js failure sayable.
 *
 * supabase-js does not throw. A failed request comes back as `{ data, error }`,
 * and that `error` is the parsed PostgREST response body -- a plain object with
 * `message`, `code`, `details` and `hint`. It is *not* an `Error` instance:
 * `PostgrestError` is only constructed on the `throwOnError()` path, which no
 * call site here uses. So the very common
 *
 *     toast.error(error instanceof Error ? error.message : 'Something failed')
 *
 * takes the fallback branch for every real database failure and throws the one
 * sentence that would have explained it away. That is not a hypothetical: the
 * **Generate more ideas** button reported "Could not start a trend run" for a
 * week while PostgREST was answering, in full, that `request_trend_run` did not
 * exist in the schema cache because the migration had not been pushed.
 *
 * `toError` is the narrow fix -- re-throw a real `Error` so the idiom above is
 * true again -- and `codeOf` keeps the code readable for the callers that
 * branch on one, since an `Error` subclass would not survive the boundary any
 * better than the plain object did.
 */

/** The shape PostgREST actually sends back, which is not an `Error`. */
interface PostgrestLikeError {
  message?: string;
  code?: string;
  details?: string | null;
  hint?: string | null;
}

/** The PostgREST/Postgres code behind a failure, wherever it is carried. */
export function codeOf(error: unknown): string | undefined {
  return (error as PostgrestLikeError | null)?.code;
}

/**
 * Codes that mean "this bundle is ahead of the database", said as such.
 *
 * The app and the schema are deployed separately -- `bun run build` and
 * `supabase db push` are two commands, run by hand, and nothing enforces their
 * order. When they drift, PostgREST is precise about it and unhelpfully
 * technical: the raw text names a function or relation the reader has no reason
 * to have heard of. These say what to do instead, and still name the object so
 * the message is actionable by whoever can fix it.
 */
const SCHEMA_DRIFT: Record<string, string> = {
  // No such function in the schema cache: a migration has not been applied.
  PGRST202: 'This feature needs a database change that has not been applied yet.',
  // undefined_function / undefined_table, the same drift seen from Postgres.
  '42883': 'This feature needs a database change that has not been applied yet.',
  '42P01': 'This feature needs a database change that has not been applied yet.',
};

/**
 * Whatever was thrown, as an `Error` that says something.
 *
 * The original is returned untouched when it is already an `Error`, so a
 * network failure, an abort, or a bug in our own code keeps its stack. Anything
 * else is wrapped, and the `code` is copied onto the wrapper so a caller that
 * branches on one -- a unique-violation treated as information rather than
 * failure, say -- still can.
 */
export function toError(error: unknown): Error {
  if (error instanceof Error) return error;

  const source = (error ?? {}) as PostgrestLikeError;
  const code = source.code;
  const drift = code ? SCHEMA_DRIFT[code] : undefined;

  const message =
    drift ??
    source.message?.trim() ??
    // Nothing usable at all. Better than "[object Object]", and the code is
    // enough to find the cause in the PostgREST logs.
    (code ? `The database refused the request (${code}).` : 'The request failed.');

  const wrapped = new Error(drift && source.message ? `${drift} (${source.message})` : message);
  return code ? Object.assign(wrapped, { code }) : wrapped;
}
