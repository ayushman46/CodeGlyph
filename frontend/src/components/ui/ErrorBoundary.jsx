import { Home, RefreshCw, RotateCcw, TriangleAlert } from 'lucide-react';
import { Component, useEffect, useRef } from 'react';
import Button from './Button';

const RELOAD_KEY = 'ci-chunk-reload-at';

/** A lazily loaded chunk failed to download, usually after a new deploy. */
export const isChunkLoadError = (error) => {
  const text = `${error?.name || ''} ${error?.message || ''}`;
  return /ChunkLoadError|Loading (CSS )?chunk|dynamically imported module|Importing a module script failed|error loading dynamically imported module|Unable to preload CSS/i.test(text);
};

const isOffline = () => typeof navigator !== 'undefined' && navigator.onLine === false;

/** Reload once per 30 s for a stale chunk; returns false if we just tried. */
const reloadForNewVersion = () => {
  if (isOffline()) return false;
  try {
    const last = Number(window.sessionStorage.getItem(RELOAD_KEY) || 0);
    if (Date.now() - last < 30000) return false;
    window.sessionStorage.setItem(RELOAD_KEY, String(Date.now()));
  } catch {
    return false;
  }
  window.location.reload();
  return true;
};

export const ErrorFallback = ({ error, onReset, fullscreen = false }) => {
  const headingRef = useRef(null);
  const offline = isChunkLoadError(error) && isOffline();
  const stale = isChunkLoadError(error) && !offline;
  useEffect(() => {
    // Land keyboard and screen-reader users on the explanation.
    headingRef.current?.focus({ preventScroll: true });
  }, []);
  const message = String(error?.message || error || 'Unknown error').slice(0, 600);
  return (
    <div className={`flex items-center justify-center px-4 ${fullscreen ? 'min-h-dvh bg-canvas text-ink' : 'min-h-[60vh] py-16'}`}>
      <div role="alert" className="w-full max-w-md rounded-3xl border border-line bg-surface p-6 text-center shadow-[var(--shadow-soft)] sm:p-8">
        <div className="mx-auto flex h-11 w-11 items-center justify-center rounded-2xl bg-warning-soft text-warning">
          {stale ? <RefreshCw className="h-5 w-5" aria-hidden="true" /> : <TriangleAlert className="h-5 w-5" aria-hidden="true" />}
        </div>
        <h1 ref={headingRef} tabIndex={-1} className="mt-4 text-lg font-semibold tracking-tight text-ink">
          {offline ? 'You are offline' : stale ? 'A newer version is available' : 'This page ran into a problem'}
        </h1>
        <p className="mt-2 text-sm leading-relaxed text-ink-2">
          {offline
            ? 'This page could not be downloaded without a connection. Reconnect, then reload.'
            : stale
              ? 'Part of the app could not be loaded, usually because it was just updated. Reloading picks up the latest version.'
              : 'Something on this page failed to display. Your indexed repositories are not affected. Try again, or reload the page if it keeps happening.'}
        </p>
        <div className="mt-6 flex flex-col-reverse justify-center gap-2 sm:flex-row">
          {/* Plain links: this fallback may render outside the router. */}
          <Button href="/" variant="ghost" size="md" icon={Home}>Go home</Button>
          {!stale && !offline && onReset && <Button variant="secondary" size="md" icon={RotateCcw} onClick={onReset}>Try again</Button>}
          <Button variant="primary" size="md" icon={RefreshCw} onClick={() => window.location.reload()}>Reload page</Button>
        </div>
        {!stale && !offline && (
          <details className="mt-6 text-left">
            <summary className="cursor-pointer select-none rounded text-[12.5px] font-medium text-ink-3 hover:text-ink">Technical details</summary>
            <pre className="code-scroll mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded-xl bg-surface-2 p-3 font-mono text-[11.5px] leading-relaxed text-ink-2" tabIndex={0}>{message}</pre>
          </details>
        )}
      </div>
    </div>
  );
};

const sameKeys = (a = [], b = []) => a.length === b.length && a.every((value, index) => Object.is(value, b[index]));

/**
 * Catches render errors below it and shows a recoverable fallback instead
 * of a blank page. Changing any of `resetKeys` (e.g. the pathname) clears
 * the error, so navigating away recovers automatically. A stale-chunk error
 * after a deploy reloads the page once by itself.
 */
class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
    this.reset = this.reset.bind(this);
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    // eslint-disable-next-line no-console
    console.error('Render error caught by ErrorBoundary', error, info?.componentStack);
    if (isChunkLoadError(error)) reloadForNewVersion();
    this.props.onError?.(error, info);
  }

  componentDidUpdate(previous) {
    if (this.state.error && !sameKeys(previous.resetKeys, this.props.resetKeys)) this.reset();
  }

  reset() {
    this.setState({ error: null });
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    if (typeof this.props.fallback === 'function') return this.props.fallback({ error, reset: this.reset });
    if (this.props.fallback) return this.props.fallback;
    return <ErrorFallback error={error} onReset={this.reset} fullscreen={this.props.fullscreen} />;
  }
}

export default ErrorBoundary;
