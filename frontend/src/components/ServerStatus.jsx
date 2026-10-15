import { Coffee } from 'lucide-react';
import { useEffect } from 'react';
import { useLocation } from 'react-router-dom';
import { health } from '../api/client';
import useStore from '../store/useStore';
import Spinner from './ui/Spinner';

/* Public pages that never call the API for a signed-out visitor. */
const MARKETING_PATHS = new Set(['/', '/platform', '/pricing', '/docs']);

/**
 * The backend may run on a free tier that sleeps after inactivity. Ping it
 * as soon as the app loads (while the user is still signing in) and explain
 * the delay instead of leaving requests silently pending.
 *
 * Someone reading the marketing pages while signed out is not waiting on the
 * server, so the wake-up ping still runs for them but the notice stays hidden
 * until they are signed in or on a page that needs the API.
 */
const ServerStatus = () => {
  const serverWaking = useStore((state) => state.serverWaking);
  const setServerWaking = useStore((state) => state.setServerWaking);
  const signedIn = useStore((state) => Boolean(state.user));
  const { pathname } = useLocation();

  useEffect(() => {
    let cancelled = false;
    const timer = window.setTimeout(() => { if (!cancelled) setServerWaking(true); }, 2500);
    health()
      .catch(() => null)
      .finally(() => {
        cancelled = true;
        window.clearTimeout(timer);
        setServerWaking(false);
      });
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [setServerWaking]);

  const visible = serverWaking && (signedIn || !MARKETING_PATHS.has(pathname));

  // The live region stays mounted so the message is announced when it
  // appears (a region inserted together with its text often is not).
  return (
    <div
      className="pointer-events-none fixed inset-x-0 bottom-0 z-[90] flex justify-center px-4 pb-[max(1rem,env(safe-area-inset-bottom))]"
      role="status"
      aria-live="polite"
    >
      {visible && (
        <div className="pointer-events-auto flex max-w-md animate-rise items-center gap-3 rounded-2xl border border-line bg-surface px-4 py-3 text-sm shadow-[var(--shadow-lift)]">
          <Coffee className="h-4 w-4 shrink-0 text-accent" aria-hidden="true" />
          <span className="text-ink-2">Waking up the server — the first request after a quiet period can take up to a minute.</span>
          <Spinner className="h-4 w-4 shrink-0 text-ink-3" />
        </div>
      )}
    </div>
  );
};

export default ServerStatus;
