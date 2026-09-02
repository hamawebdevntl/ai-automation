import type { Session, User } from '@supabase/supabase-js';
import { createContext, use, useCallback, useEffect, useMemo, useState } from 'react';
import { siteUrl } from '@/lib/env';
import { supabase } from '@/lib/supabase';

export interface AuthContextValue {
  session: Session | null;
  user: User | null;
  isAuthenticated: boolean;
  signInWithPassword: (email: string, password: string) => Promise<void>;
  signUpWithPassword: (email: string, password: string) => Promise<{ needsEmailConfirmation: boolean }>;
  signInWithGoogle: (redirectTo?: string) => Promise<void>;
  sendPasswordReset: (email: string) => Promise<void>;
  updatePassword: (password: string) => Promise<void>;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

/** Turns a Supabase error into something worth showing a person. */
function assertOk(error: { message: string } | null): void {
  if (error) throw new Error(error.message);
}

export function AuthProvider({ children, fallback }: { children: React.ReactNode; fallback?: React.ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [isResolved, setIsResolved] = useState(false);

  useEffect(() => {
    let active = true;

    // `detectSessionInUrl` may still be exchanging an OAuth code when this
    // runs, which is why the first read is awaited before anything renders.
    supabase.auth.getSession().then(({ data }) => {
      if (!active) return;
      setSession(data.session);
      setIsResolved(true);
    });

    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((_event, nextSession) => {
      setSession(nextSession);
      setIsResolved(true);
    });

    return () => {
      active = false;
      subscription.unsubscribe();
    };
  }, []);

  const signInWithPassword = useCallback(async (email: string, password: string) => {
    const { error } = await supabase.auth.signInWithPassword({ email, password });
    assertOk(error);
  }, []);

  const signUpWithPassword = useCallback(async (email: string, password: string) => {
    const { data, error } = await supabase.auth.signUp({
      email,
      password,
      options: { emailRedirectTo: `${siteUrl()}/login` },
    });
    assertOk(error);
    return { needsEmailConfirmation: data.session === null };
  }, []);

  const signInWithGoogle = useCallback(async (redirectTo?: string) => {
    const target = new URL(redirectTo ?? '/queue', siteUrl());
    const { error } = await supabase.auth.signInWithOAuth({
      provider: 'google',
      options: { redirectTo: target.toString() },
    });
    assertOk(error);
  }, []);

  const sendPasswordReset = useCallback(async (email: string) => {
    const { error } = await supabase.auth.resetPasswordForEmail(email, {
      redirectTo: `${siteUrl()}/reset-password`,
    });
    assertOk(error);
  }, []);

  const updatePassword = useCallback(async (password: string) => {
    const { error } = await supabase.auth.updateUser({ password });
    assertOk(error);
  }, []);

  const signOut = useCallback(async () => {
    const { error } = await supabase.auth.signOut();
    assertOk(error);
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      session,
      user: session?.user ?? null,
      isAuthenticated: session !== null,
      signInWithPassword,
      signUpWithPassword,
      signInWithGoogle,
      sendPasswordReset,
      updatePassword,
      signOut,
    }),
    [session, signInWithPassword, signUpWithPassword, signInWithGoogle, sendPasswordReset, updatePassword, signOut],
  );

  // Route guards read `isAuthenticated` synchronously in `beforeLoad`, so
  // nothing may render until the first session read has settled — otherwise
  // every reload bounces through /login.
  if (!isResolved) return fallback ?? null;

  return <AuthContext value={value}>{children}</AuthContext>;
}

export function useAuth(): AuthContextValue {
  const context = use(AuthContext);
  if (!context) throw new Error('useAuth must be used inside <AuthProvider>');
  return context;
}
