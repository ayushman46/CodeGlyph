import {
  CircleAlert as CircleAlertIcon,
  FileText as FileTextIcon,
  LoaderCircle as LoaderCircleIcon,
  PanelRight as PanelRightIcon,
  X as CloseIcon,
} from 'lucide-react';
import { forwardRef, useEffect, useId, useMemo, useRef } from 'react';
import { cn } from '../../lib/cn';
import Button from '../ui/Button';
import { mergeRefs } from '../ui/Popover';

/*
 * Artifact Panel + Artifact Card (Honest UI port).
 *
 * The card sits in a reply and opens the panel; the panel shows the artifact
 * (a diff, a file, a document) beside the conversation. The panel is NOT a
 * dialog: no focus trap and no Escape close. It fills its parent's width and
 * height, so give the parent a height for the body to scroll. Common layout:
 * two columns on wide screens; on narrow ones the panel takes the
 * conversation's place and Close leads back. When a narrow layout covers the
 * conversation with the panel, hide the conversation from assistive
 * technology (inert / aria-hidden) while it is covered.
 *
 * Focus: pass `autoFocus` when a person opened the panel (not when a reply
 * did), and in `onClose` return focus to the card that opened it (the card
 * forwards its ref for that).
 */

/** @typedef {'streaming' | 'complete' | 'error'} ArtifactStatus */

/**
 * @typedef {object} ArtifactLabels
 * @property {string} open      Card hint while the panel is closed.
 * @property {string} showing   Card hint while the panel is open.
 * @property {string} close     Accessible name and tooltip of the panel's Close button.
 * @property {string} streaming Status text while the artifact is being written.
 * @property {string} error     Shown when the artifact stopped before it finished.
 * @property {string} retry     The panel's retry button.
 */

/** @type {ArtifactLabels} */
export const defaultArtifactLabels = {
  open: 'Open',
  showing: 'Showing',
  close: 'Close',
  streaming: 'Writing…',
  error: 'This stopped before it finished.',
  retry: 'Try again',
};

// Honest UI `smallText` (12px / 16px) and `miniText` (11px / 14px).
const smallText = 'text-xs';
const miniText = 'text-[11px] leading-3.5';

/**
 * The artifact, in a <section> named by its title. `aria-busy` while
 * streaming; an always-mounted `role="status"` announces "Writing…" coming
 * and going. Error status adds a `role="alert"` row under the header (with
 * Try again when `onRetry` is given). The body is a focusable, scrolling
 * `role="group"` so its content can be scrolled by keyboard.
 *
 * @typedef {object} ArtifactPanelProps
 * @property {import('react').ReactNode} title
 * @property {import('react').ReactNode} [description]
 * @property {ArtifactStatus} [status='complete']
 * @property {import('react').ReactNode} [actions] Header buttons placed before Close.
 * @property {() => void} [onClose] Shows the Close button; return focus to the opening card here.
 * @property {() => void} [onRetry] Shows Try again in the error row.
 * @property {boolean} [flush=false] Drops the body padding (edge-to-edge content).
 * @property {boolean} [autoFocus=false] Focus the panel once, when it mounts.
 * @property {Partial<ArtifactLabels>} [labels]
 * @property {string} [className]
 * @property {import('react').ReactNode} [children]
 */

/** @type {import('react').ForwardRefExoticComponent<ArtifactPanelProps & Omit<import('react').HTMLAttributes<HTMLElement>, 'title'> & import('react').RefAttributes<HTMLElement>>} */
export const ArtifactPanel = /* @__PURE__ */ forwardRef(function ArtifactPanel({
  title,
  description,
  status = 'complete',
  actions,
  onClose,
  onRetry,
  flush = false,
  autoFocus = false,
  labels: labelsProp,
  className,
  children,
  ...props
}, ref) {
  const labels = { ...defaultArtifactLabels, ...labelsProp };
  const rootRef = useRef(null);
  const setRootRef = useMemo(() => mergeRefs(rootRef, ref), [ref]);
  const titleId = useId();

  // Focus moves once, when the panel appears (not when `autoFocus` changes).
  useEffect(() => {
    if (autoFocus) rootRef.current?.focus();
  }, []);

  return (
    <section
      ref={setRootRef}
      aria-labelledby={titleId}
      aria-busy={status === 'streaming'}
      tabIndex={-1}
      data-slot="artifact-panel"
      data-status={status}
      // The reference's focus-visible ring. The app's global ring skips
      // tabIndex=-1 containers, so it is drawn here: a keyboard user who
      // opens the panel sees focus arrive (a pointer-opened panel does not
      // match :focus-visible and stays ring-free).
      className={cn(
        'flex h-full min-h-0 w-full min-w-0 flex-col overflow-hidden rounded-2xl border border-line bg-surface text-ink focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent',
        smallText,
        className,
      )}
      {...props}
    >
      <div
        data-slot="artifact-panel-header"
        className="flex min-w-0 shrink-0 items-center gap-2 border-b border-line py-1 ps-3 pe-1"
      >
        <div className="min-w-0 flex-1">
          <p id={titleId} className="m-0 truncate font-medium">{title}</p>
          {description && (
            <p className={cn('m-0 truncate text-ink-2', miniText)}>{description}</p>
          )}
        </div>
        {/* Always mounted, so the change to and from writing is announced. */}
        <span role="status" className="flex shrink-0 items-center gap-1 text-ink-2">
          {status === 'streaming' && (
            <>
              <LoaderCircleIcon aria-hidden="true" className="size-3.5 shrink-0 motion-safe:animate-spin" />
              {labels.streaming}
            </>
          )}
        </span>
        {(actions || onClose) && (
          <div className="flex shrink-0 items-center gap-0.5">
            {actions}
            {onClose && (
              <Button variant="link" size="icon-sm" aria-label={labels.close} title={labels.close} onClick={onClose}>
                <CloseIcon aria-hidden="true" />
              </Button>
            )}
          </div>
        )}
      </div>
      {status === 'error' && (
        <div
          role="alert"
          className="flex min-w-0 shrink-0 flex-wrap items-center gap-x-2 gap-y-0.5 border-b border-line py-1 ps-3 pe-1"
        >
          <span className="flex min-w-0 flex-1 items-start gap-1">
            <CircleAlertIcon
              strokeWidth={1.5}
              strokeLinecap="round"
              aria-hidden="true"
              className="mt-px size-3.5 shrink-0 text-danger"
            />
            <span className="min-w-0 break-words">{labels.error}</span>
          </span>
          {onRetry && (
            <Button variant="link" size="sm" className="shrink-0" onClick={onRetry}>{labels.retry}</Button>
          )}
        </div>
      )}
      {/* Focusable so the content can be scrolled by keyboard. */}
      <div
        role="group"
        aria-labelledby={titleId}
        tabIndex={0}
        data-slot="artifact-panel-body"
        className={cn('min-h-0 flex-1 overflow-auto focus-visible:outline-offset-[-2px]', !flush && 'p-3')}
      >
        {children}
      </div>
    </section>
  );
});

/**
 * The button in a reply that opens the artifact. `aria-expanded` mirrors
 * whether its panel is open (and draws the accent border). The icon is a
 * spinner while streaming, an alert on error, else `icon` (a file by
 * default); the note is the status text while unfinished, else
 * `description`. Forwards its ref so `onClose` can return focus to it.
 *
 * @typedef {object} ArtifactCardProps
 * @property {import('react').ReactNode} title
 * @property {import('react').ReactNode} [description]
 * @property {ArtifactStatus} [status='complete']
 * @property {boolean} [isOpen=false]
 * @property {import('react').ReactNode} [icon] Replaces the file icon when complete.
 * @property {Partial<ArtifactLabels>} [labels]
 * @property {string} [className]
 */

/** @type {import('react').ForwardRefExoticComponent<ArtifactCardProps & Omit<import('react').ButtonHTMLAttributes<HTMLButtonElement>, 'title'> & import('react').RefAttributes<HTMLButtonElement>>} */
export const ArtifactCard = /* @__PURE__ */ forwardRef(function ArtifactCard({
  title,
  description,
  status = 'complete',
  isOpen = false,
  icon,
  labels: labelsProp,
  className,
  ...props
}, ref) {
  const labels = { ...defaultArtifactLabels, ...labelsProp };
  const note = status === 'streaming' ? labels.streaming : status === 'error' ? labels.error : description;
  return (
    <button
      ref={ref}
      type="button"
      aria-expanded={isOpen}
      data-slot="artifact-card"
      data-status={status}
      className={cn(
        'flex w-full max-w-sm min-w-0 cursor-pointer items-center gap-2 rounded-xl border border-line bg-surface px-3 py-2 text-start text-ink shadow-xs motion-safe:transition-interactive hover:bg-surface-2 aria-expanded:border-accent',
        smallText,
        className,
      )}
      {...props}
    >
      <span aria-hidden="true" className="flex size-4 shrink-0 items-center justify-center text-ink-2 [&_svg]:size-4">
        {status === 'streaming' ? (
          <LoaderCircleIcon className="motion-safe:animate-spin" />
        ) : status === 'error' ? (
          <CircleAlertIcon strokeWidth={1.5} strokeLinecap="round" />
        ) : (
          icon ?? <FileTextIcon />
        )}
      </span>
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="truncate font-medium">{title}</span>
        {note && <span className={cn('truncate text-ink-2', miniText)}>{note}</span>}
      </span>
      <span className={cn('flex shrink-0 items-center gap-1 text-ink-2', miniText)}>
        {isOpen ? labels.showing : labels.open}
        <PanelRightIcon aria-hidden="true" className="size-3.5 shrink-0" />
      </span>
    </button>
  );
});

export default ArtifactPanel;
