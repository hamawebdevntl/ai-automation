import { describe, expect, it } from 'vitest';
import { env } from '@/lib/env';
import { createClient, supabase } from '@/lib/supabase';

describe('supabase client', () => {
  it('parses the environment it was given', () => {
    expect(env?.VITE_SUPABASE_URL).toBe('https://test-project.supabase.co');
  });

  it('constructs lazily and only once', () => {
    expect(createClient()).toBe(createClient());
  });

  it('builds a query through the proxy accessor', () => {
    // The accessor binds methods to the real client. If it handed them back
    // loose, `this` would be the proxy and this call would throw — which is the
    // failure mode this test exists to catch.
    const builder = supabase.from('ideas').select('*').eq('status', 'pending');
    expect(builder).toBeDefined();
    expect(typeof builder.then).toBe('function');
  });

  it('reaches nested clients without going through the proxy for their methods', () => {
    expect(typeof supabase.auth.getSession).toBe('function');
    expect(typeof supabase.rpc).toBe('function');
  });
});
