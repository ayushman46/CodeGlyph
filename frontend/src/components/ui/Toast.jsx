import { CircleAlert, CircleCheck, Info, TriangleAlert, WifiOff, X } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { dismissToast, toast, useAnnouncement, useToasts } from '../../hooks/useToasts';
import Spinner from './Spinner';

export { toast, announce, dismissToast } from '../../hooks/useToasts';

const TONES = {
  neutral: { icon: Info, color: 'text-ink-3' },
  success: { icon: CircleCheck, color: 'text-success' },
  warning: { icon: TriangleAlert, color: 'text-warning' },
  danger: { icon: CircleAlert, color: 'text-danger' },
  offline: { icon: WifiOff, color: 'text-warning' },
  loading: { icon: null, color: 'text-ink-3' },
};

const ToastItem = ({ item }) => {
  const [paused, setPaused] = useState(false);
  const remaining = useRef(item.duration);
  const startedAt = useRef(0);

  // Restart the clock when the toast is replaced in place (same id).
  useEffect(() => {
    remaining.current = item.duration;
  }, [item.createdAt, item.duration]);

  useEffect(() => {
    if (paused || !Number.isFinite(remaining.current)) return undefined;
    startedAt.current = Date.now();
    const timer = window.setTimeout(() => dismissToast(item.id), Math.max(0, remaining.current));
    return () => {
      window.clearTimeout(timer);
      remaining.current -= Date.now() - startedAt.current;
    };
  }, [paused, item.id, item.createdAt]);

  const tone = TONES[item.tone] || TONES.neutral;
  const Icon = tone.icon;
  return (
    <li
      className="pointer-events-auto flex w-full animate-rise items-start gap-3 rounded-2xl border border-line bg-surface py-3 pl-3.5 pr-2 text-left shadow-[var(--shadow-lift)] dark:border-line-strong"
      onMouseEnter={() => setPaused(true)}
      onMouseLeave={() => setPaused(false)}
      onFocus={() => setPaused(true)}
      onBlur={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) setPaused(false); }}
    >
      <span className={`mt-0.5 shrink-0 ${tone.color}`} aria-hidden="true">
        {Icon ? <Icon className="h-4 w-4" /> : <Spinner className="h-4 w-4" />}
      </span>
      <div className="min-w-0 flex-1 py-px">
        <p className="text-[13.5px] font-medium leading-snug text-ink">{item.title}</p>
        {item.description && <p className="mt-0.5 text-[12.5px] leading-relaxed text-ink-3">{item.description}</p>}
        {item.action && (
          <button
            type="button"
            onClick={() => { item.action.onClick?.(); dismissToast(item.id); }}
            className="mt-2 inline-flex h-7 items-center rounded-lg border border-line bg-surface px-2.5 text-xs font-medium text-ink transition-colors duration-(--duration-fast) hover:border-line-strong hover:bg-surface-2 pointer-coarse:h-9"
          >
            {item.action.label}
          </button>
        )}
      </div>
      {item.dismissible !== false && (
        <button
          type="button"
          onClick={() => dismissToast(item.id)}
          className="flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-ink-3 transition-colors duration-(--duration-fast) hover:bg-surface-2 hover:text-ink pointer-coarse:h-9 pointer-coarse:w-9"
          aria-label="Dismiss notification"
        >
          <X className="h-3.5 w-3.5" aria-hidden="true" />
        </button>
      )}
    </li>
  );
};

/** Says when the browser loses or regains its network connection. */
const useConnectionToasts = (enabled) => {
  useEffect(() => {
    if (!enabled) return undefined;
    const offline = () => toast({
      id: 'connection',
      tone: 'offline',
      title: 'You are offline',
      description: 'Questions and changes will not go through until the connection is back.',
      duration: Infinity,
    });
    const online = () => {
      dismissToast('connection');
      toast({ id: 'connection-back', tone: 'success', title: 'Back online', duration: 3000 });
    };
    if (typeof navigator !== 'undefined' && navigator.onLine === false) offline();
    window.addEventListener('offline', offline);
    window.addEventListener('online', online);
    return () => {
      window.removeEventListener('offline', offline);
      window.removeEventListener('online', online);
    };
  }, [enabled]);
};

/**
 * Renders toasts (top centre, under the header, so they never cover the
 * composer or the bottom sheets on phones) plus the two visually hidden
 * live regions used by `announce()`. Mount once.
 */
const Toaster = ({ watchConnection = true }) => {
  const items = useToasts();
  const { polite, assertive } = useAnnouncement();
  useConnectionToasts(watchConnection);
  return (
    <>
      <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">{polite}</div>
      <div className="sr-only" aria-live="assertive" aria-atomic="true">{assertive}</div>
      {items.length > 0 && (
        <section
          aria-label="Notifications"
          className="pointer-events-none fixed inset-x-0 top-[calc(env(safe-area-inset-top)+4rem)] z-[95] flex justify-center px-4"
        >
          <ol className="flex w-full max-w-sm flex-col gap-2">
            {items.map((item) => <ToastItem key={item.id} item={item} />)}
          </ol>
        </section>
      )}
    </>
  );
};

export default Toaster;
