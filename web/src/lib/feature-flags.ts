import { env } from '@/lib/env';

/**
 * Feature flags, read from the Vite environment (`VITE_*`).
 *
 * These are *presentation* switches only. Every flag here ships inside the
 * JavaScript bundle where a user can flip it, so none of them is a security
 * boundary. The Next.js app also carried `AUTH_ALLOW_GOOGLE_SIGNUP`, which was
 * enforced server-side; it is deliberately absent here rather than
 * reimplemented as something a browser could bypass. Restricting who may sign
 * up belongs in Supabase (Authentication -> Providers, or a database trigger
 * on `auth.users`).
 */

/** Master switch for the auth pages. */
export const isAuthEnabled = () => env?.VITE_AUTH_ENABLED ?? false;

/** Email + password form on the login page. */
export const isLoginEmailAuthEnabled = () => env?.VITE_LOGIN_EMAIL_AUTH_ENABLED ?? false;

/** Email + password form on the register page. */
export const isRegisterEmailAuthEnabled = () => env?.VITE_REGISTER_EMAIL_AUTH_ENABLED ?? false;

/** Forgot-password page. */
export const isForgotPasswordEnabled = () => env?.VITE_FORGOT_PASSWORD_ENABLED ?? false;

/** Reset-password page. */
export const isResetPasswordEnabled = () => env?.VITE_RESET_PASSWORD_ENABLED ?? false;

/** Google OAuth button. */
export const isGoogleAuthEnabled = () => env?.VITE_GOOGLE_AUTH_ENABLED ?? false;
