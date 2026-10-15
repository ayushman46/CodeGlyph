import { CheckCircle2, CircleSlash, Github, LogIn, XCircle } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { completeGithubConnection, errorMessage } from '../api/client';
import Button from '../components/ui/Button';
import { LogoMark } from '../components/ui/Logo';
import Spinner from '../components/ui/Spinner';
import useStore from '../store/useStore';

// One completion per authorization: the server rejects a second use of the
// same state. Module scope survives React StrictMode's double effects.
const handledStates = new Set();
const CANCELLED_MESSAGE = 'GitHub authorization was cancelled.';
// Routes the server may send the user back to (see _safe_redirect).
const DESTINATIONS = {
  '/account': 'Back to your account',
  '/pricing': 'Back to pricing',
  '/app': 'Back to your workspace',
  '/': 'Back to Codebase Intel',
};

const readFragment = () => {
  const params = new URLSearchParams(window.location.hash.replace(/^#/, ''));
  return {
    code: params.get('code') || '',
    state: params.get('state') || '',
    error: params.get('error') || '',
    present: Boolean(window.location.hash.replace(/^#/, '')),
  };
};

const notifyOpener = (payload) => {
  try {
    if (window.opener && !window.opener.closed) window.opener.postMessage(payload, window.location.origin);
  } catch {
    // The opener may be gone; the review window also polls /github/status.
  }
};

const safeDestination = (value) => (
  typeof value === 'string' && Object.prototype.hasOwnProperty.call(DESTINATIONS, value) ? value : '/app'
);

const STATES = {
  working: { icon: null, title: 'Connecting GitHub…', tone: 'text-ink-2' },
  done: { icon: CheckCircle2, title: 'GitHub connected', tone: 'text-success' },
  cancelled: { icon: CircleSlash, title: 'Connection cancelled', tone: 'text-ink-3' },
  error: { icon: XCircle, title: 'GitHub connection failed', tone: 'text-danger' },
  signedOut: { icon: LogIn, title: 'Sign in to finish connecting', tone: 'text-warning' },
  idle: { icon: Github, title: 'Nothing to connect here', tone: 'text-ink-3' },
};

/**
 * Landing page of the GitHub OAuth popup (route /github/callback).
 *
 * The authorization code arrives in the URL fragment, never in a query
 * string or Referer. It is sent with this browser's own session to
 * POST /api/github/complete, which links GitHub only for the user who
 * started the flow, so a link crafted by someone else cannot attach the
 * victim's GitHub account to the attacker's Codebase Intel account.
 */
const GithubCallbackPage = () => {
  const userId = useStore((state) => state.user?.id);
  // Read the fragment during the first render (a pure read) so a re-run of
  // the effect, after the fragment has been cleared from the address bar,
  // still sees the original authorization response.
  const fragmentRef = useRef(null);
  if (fragmentRef.current === null) fragmentRef.current = readFragment();
  const [status, setStatus] = useState({ phase: fragmentRef.current.present ? 'working' : 'idle', message: '', username: '' });
  const [destination, setDestination] = useState('/app');
  const [closeFailed, setCloseFailed] = useState(false);
  // StrictMode (and a refreshed session object) can run the effect again;
  // the response must be handled, and the opener told, exactly once.
  const ranRef = useRef(false);
  const isPopup = Boolean(window.opener);

  useEffect(() => {
    if (window.location.hash) window.history.replaceState(null, '', window.location.pathname);
  }, []);

  useEffect(() => {
    const { code, state, error, present } = fragmentRef.current;
    if (!present || ranRef.current) return;
    ranRef.current = true;
    if (error || !code || !state) {
      const cancelled = error === CANCELLED_MESSAGE;
      const message = error || 'The GitHub authorization response was incomplete.';
      setStatus({ phase: cancelled ? 'cancelled' : 'error', message, username: '' });
      notifyOpener({ type: 'GITHUB_AUTH_ERROR', error: message });
      return;
    }
    if (handledStates.has(state)) return;
    handledStates.add(state);
    if (!userId) {
      const message = 'This browser is not signed in to Codebase Intel, so GitHub could not be linked to your account.';
      setStatus({ phase: 'signedOut', message, username: '' });
      notifyOpener({ type: 'GITHUB_AUTH_ERROR', error: 'Sign in to Codebase Intel in this browser, then start the GitHub connection again.' });
      return;
    }
    completeGithubConnection(code, state)
      .then((response) => {
        const username = response.data?.github_username || '';
        setDestination(safeDestination(response.data?.redirect_to));
        setStatus({ phase: 'done', message: '', username });
        notifyOpener({ type: 'GITHUB_AUTH_SUCCESS', github_username: username });
        if (window.opener) window.setTimeout(() => { try { window.close(); } catch { /* ignore */ } }, 1200);
      })
      .catch((requestError) => {
        const message = errorMessage(requestError, 'GitHub connection could not be completed.');
        setStatus({ phase: 'error', message, username: '' });
        notifyOpener({ type: 'GITHUB_AUTH_ERROR', error: message });
      });
    // The response is read once; only a change of account re-runs this.
  }, [userId]);

  const { phase } = status;
  const view = STATES[phase];
  const Icon = view.icon;

  useEffect(() => {
    const previous = document.title;
    document.title = `${view.title.replace('…', '')} · Codebase Intel`;
    return () => {
      document.title = previous;
    };
  }, [view.title]);

  const closeWindow = () => {
    try {
      window.close();
    } catch {
      // Fall through to the hint below.
    }
    window.setTimeout(() => setCloseFailed(true), 300);
  };

  const description = {
    working: 'Finishing the authorization with your Codebase Intel account. This takes a few seconds.',
    done: status.username
      ? <>Signed in to GitHub as <span className="font-mono font-medium text-ink">@{status.username}</span>. You can open pull requests from any reviewed change.</>
      : 'You can open pull requests from any reviewed change.',
    cancelled: 'Nothing was connected. You can start again from the review screen or your account page whenever you are ready.',
    error: status.message,
    signedOut: status.message,
    idle: 'This page finishes a GitHub connection started from Codebase Intel. To connect GitHub, use your account page or the review screen of a proposed change.',
  }[phase];

  const nextStep = {
    error: 'Start the connection again from the review screen or your account page. If it keeps failing, the message above says what the server needs.',
    signedOut: 'Sign in with Google in this browser, then start the GitHub connection again from the review screen.',
  }[phase];

  const backTo = phase === 'signedOut' ? '/' : phase === 'idle' ? (userId ? '/account' : '/') : destination;
  const backLabel = phase === 'signedOut' ? 'Go to sign-in' : phase === 'idle' && userId ? DESTINATIONS['/account'] : DESTINATIONS[backTo] || DESTINATIONS['/app'];

  return (
    <main className="flex min-h-screen items-center justify-center bg-canvas px-4 py-10 text-ink">
      <div className="w-full max-w-md">
        <div className="mb-6 flex items-center justify-center gap-2 text-[13px] font-medium text-ink-2">
          <LogoMark className="h-6 w-6" />
          Codebase Intel
          <span className="text-ink-3" aria-hidden="true">·</span>
          <Github className="h-4 w-4" aria-hidden="true" />
          GitHub
        </div>
        <div className="rounded-3xl border border-line bg-surface p-6 text-center shadow-[var(--shadow-soft)] sm:p-8">
          <div className={`mx-auto flex h-12 w-12 items-center justify-center rounded-full bg-surface-2 ${view.tone}`}>
            {Icon ? <Icon className="h-6 w-6" aria-hidden="true" /> : <Spinner className="h-5 w-5" />}
          </div>
          <h1 className="mt-4 text-xl font-semibold tracking-tight text-ink">{view.title}</h1>
          {/* Errors interrupt; progress and success are announced politely. */}
          <div role={phase === 'error' || phase === 'signedOut' ? 'alert' : 'status'} aria-live={phase === 'error' || phase === 'signedOut' ? 'assertive' : 'polite'}>
            <p className="mt-2 break-words text-sm leading-relaxed text-ink-2">{description}</p>
            {nextStep && <p className="mt-3 text-[13px] leading-relaxed text-ink-3">{nextStep}</p>}
            {phase === 'done' && isPopup && !closeFailed && (
              <p className="mt-3 text-[13px] text-ink-3">This window closes on its own.</p>
            )}
          </div>

          {phase !== 'working' && (
            <div className="mt-6 flex flex-col items-stretch gap-2 sm:flex-row sm:justify-center">
              {isPopup && (
                <Button variant="dark" onClick={closeWindow}>Close this window</Button>
              )}
              <Button to={backTo} variant={isPopup ? 'ghost' : 'dark'}>{backLabel}</Button>
            </div>
          )}
          {closeFailed && (
            <p className="mt-3 text-[13px] text-ink-3" role="status">The browser kept this window open. You can close the tab yourself.</p>
          )}
        </div>
      </div>
    </main>
  );
};

export default GithubCallbackPage;
