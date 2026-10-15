import { AlertCircle, X } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useLocation } from 'react-router-dom';
import useStore, { POST_LOGIN_KEY } from '../store/useStore';
import Button from './ui/Button';

// If the browser has not left the page this long after the OAuth redirect
// started, release the button so a stalled redirect is never a dead end.
const REDIRECT_STALL_MS = 12000;

/** Only same-origin app paths are valid post-login destinations. */
export const safeReturnPath = (value) => (
  typeof value === 'string' && value.startsWith('/') && !value.startsWith('//') && !value.includes('\\') ? value : ''
);

/**
 * Google sign-in with honest progress: the store's isSigningIn flag clears as
 * soon as Supabase hands the browser the Google URL, before the page actually
 * unloads, so without a local "redirecting" state the button flashes back to
 * idle and invites a second click. Also forwards the page RequireAuth sent the
 * visitor away from, so they come back to it after signing in.
 */
export const useGoogleSignIn = () => {
  const signInWithGoogle = useStore((state) => state.signInWithGoogle);
  const isSigningIn = useStore((state) => state.isSigningIn);
  const authError = useStore((state) => state.authError);
  const clearAuthError = useStore((state) => state.clearAuthError);
  const location = useLocation();
  const [redirecting, setRedirecting] = useState(false);
  const [attempted, setAttempted] = useState(false);
  const mountedRef = useRef(true);
  const returnTo = safeReturnPath(location.state?.from);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    if (!redirecting) return undefined;
    // Pressing Back on Google's page restores this one from the back/forward
    // cache with the old state; release the button when that happens.
    const onPageShow = (event) => {
      if (event.persisted) setRedirecting(false);
    };
    window.addEventListener('pageshow', onPageShow);
    const timer = window.setTimeout(() => setRedirecting(false), REDIRECT_STALL_MS);
    return () => {
      window.removeEventListener('pageshow', onPageShow);
      window.clearTimeout(timer);
    };
  }, [redirecting]);

  const start = async () => {
    if (redirecting || isSigningIn) return false;
    setAttempted(true);
    const started = await signInWithGoogle();
    if (started) {
      if (returnTo) {
        try {
          window.sessionStorage.setItem(POST_LOGIN_KEY, returnTo);
        } catch {
          // Storage can be unavailable; the user lands on the workspace instead.
        }
      }
      if (mountedRef.current) setRedirecting(true);
    }
    return started;
  };

  const dismissError = () => {
    clearAuthError();
    setAttempted(false);
  };

  // Progress and errors belong to the button that was pressed; start() still
  // ignores clicks anywhere while another sign-in is underway.
  return {
    start,
    busy: (attempted && isSigningIn) || redirecting,
    error: attempted ? authError : null,
    dismissError,
  };
};

const GoogleIcon = () => (
  <svg className="h-4 w-4" viewBox="0 0 24 24" aria-hidden="true">
    <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 0 1-2.2 3.32v2.77h3.57c2.08-1.92 3.27-4.74 3.27-8.1z" />
    <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84A11 11 0 0 0 12 23z" />
    <path fill="#FBBC05" d="M5.84 14.1A6.6 6.6 0 0 1 5.5 12c0-.73.13-1.44.34-2.1V7.06H2.18A11 11 0 0 0 1 12c0 1.77.42 3.45 1.18 4.94l3.66-2.84z" />
    <path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1A11 11 0 0 0 2.18 7.06l3.66 2.84C6.71 7.31 9.14 5.38 12 5.38z" />
  </svg>
);

/**
 * The error belongs to whichever button was pressed, and that button may sit
 * in the header or deep in a page, so it is shown as a toast just below the
 * header instead of inline (which would break the callers' flex layouts).
 */
const SignInErrorToast = ({ message, onDismiss, onRetry }) => createPortal(
  <div className="pointer-events-none fixed inset-x-0 top-[4.25rem] z-[95] flex justify-center px-4 sm:justify-end sm:px-6">
    <div role="alert" className="pointer-events-auto flex w-full max-w-sm animate-rise items-start gap-3 rounded-2xl border border-danger/25 bg-surface p-4 text-sm shadow-[var(--shadow-lift)]">
      <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-danger" aria-hidden="true" />
      <div className="min-w-0 flex-1">
        <p className="font-semibold text-ink">Google sign-in did not start</p>
        <p className="mt-1 break-words leading-relaxed text-ink-2">{message}</p>
        <button type="button" onClick={onRetry} className="mt-2 text-[13px] font-medium text-accent-strong underline-offset-2 hover:underline">
          Try again
        </button>
      </div>
      <button
        type="button"
        onClick={onDismiss}
        className="-m-1 flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-ink-3 transition-colors hover:bg-surface-2 hover:text-ink"
        aria-label="Dismiss sign-in error"
      >
        <X className="h-4 w-4" aria-hidden="true" />
      </button>
    </div>
  </div>,
  document.body,
);

const GoogleSignInButton = ({ compact = false, size = 'md', className = '', label = 'Continue with Google' }) => {
  const { start, busy, error, dismissError } = useGoogleSignIn();
  const toast = error ? <SignInErrorToast message={error} onDismiss={dismissError} onRetry={() => void start()} /> : null;

  if (compact) {
    return (
      <>
        <Button variant="dark" size="sm" onClick={() => void start()} loading={busy} className={className}>
          Sign in
          {busy && <span className="sr-only">: redirecting to Google</span>}
        </Button>
        {toast}
      </>
    );
  }
  return (
    <>
      <Button variant="secondary" size={size} onClick={() => void start()} loading={busy} className={`shadow-sm ${className}`}>
        {!busy && <GoogleIcon />}
        {busy ? 'Redirecting to Google…' : label}
      </Button>
      {toast}
    </>
  );
};

export default GoogleSignInButton;
