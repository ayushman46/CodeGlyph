import { useSyncExternalStore } from 'react';

/*
 * App-wide toasts and screen-reader announcements. Anything can call
 * `toast(...)` or `announce(...)` (no provider or hook needed);
 * <Toaster /> (components/ui/Toast.jsx), mounted once in App, renders them.
 *
 *   toast('Copied to clipboard')
 *   toast.success('Pull request #12 created', { action: { label: 'Open', onClick } })
 *   toast.error('Could not delete the repository', { description: message })
 *   const id = toast.loading('Exporting…'); ...; toast.dismiss(id)
 *   announce('3 results')                 // polite, nothing visible
 *   announce('Connection lost', 'assertive')
 */

const DEFAULT_DURATION = { danger: 8000, warning: 7000, loading: Infinity };
const MAX_VISIBLE = 3;

let toasts = [];
let announcement = { polite: '', assertive: '', id: 0 };
let sequence = 0;
const listeners = new Set();

const emit = () => listeners.forEach((listener) => listener());
const subscribe = (listener) => {
  listeners.add(listener);
  return () => listeners.delete(listener);
};

/** Read a message to screen-reader users without showing anything. */
export const announce = (message, politeness = 'polite') => {
  if (!message) return;
  const key = politeness === 'assertive' ? 'assertive' : 'polite';
  sequence += 1;
  // A changing invisible suffix makes repeated identical messages re-announce.
  const text = `${message}${sequence % 2 ? '' : ' '}`;
  announcement = { ...announcement, [key]: text, id: sequence };
  emit();
};

export const dismissToast = (id) => {
  const next = toasts.filter((item) => item.id !== id);
  if (next.length === toasts.length) return;
  toasts = next;
  emit();
};

/**
 * Show a toast. `input` is a title string or an options object:
 * { title, description, tone: 'neutral'|'success'|'warning'|'danger'|'loading',
 *   action: { label, onClick }, duration (ms, Infinity to persist), id }.
 * Re-using an `id` updates that toast in place. Returns the id.
 */
export const toast = (input, options = {}) => {
  const base = typeof input === 'string' ? { title: input } : { ...input };
  const item = { tone: 'neutral', ...base, ...options };
  item.id = item.id ?? `toast-${(sequence += 1)}`;
  item.duration = item.duration ?? DEFAULT_DURATION[item.tone] ?? 5000;
  item.createdAt = Date.now();
  const exists = toasts.some((existing) => existing.id === item.id);
  toasts = exists
    ? toasts.map((existing) => (existing.id === item.id ? item : existing))
    : [...toasts, item].slice(-MAX_VISIBLE);
  emit();
  const spoken = [item.title, item.description].filter(Boolean).join('. ');
  if (item.announce !== false) announce(spoken, item.tone === 'danger' ? 'assertive' : 'polite');
  return item.id;
};

toast.success = (title, options) => toast({ ...options, title, tone: 'success' });
toast.error = (title, options) => toast({ ...options, title, tone: 'danger' });
toast.warning = (title, options) => toast({ ...options, title, tone: 'warning' });
toast.loading = (title, options) => toast({ ...options, title, tone: 'loading' });
toast.dismiss = dismissToast;

const getToasts = () => toasts;
const getAnnouncement = () => announcement;

/** Current toasts (for the Toaster). */
export const useToasts = () => useSyncExternalStore(subscribe, getToasts, getToasts);

/** Current live-region text (for the Toaster's announcer). */
export const useAnnouncement = () => useSyncExternalStore(subscribe, getAnnouncement, getAnnouncement);

export default toast;
