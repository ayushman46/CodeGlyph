import {
  Check as CheckIcon, ChevronDown as ChevronDownIcon, CircleAlert as CircleAlertIcon, Copy as CopyIcon,
  CornerDownRight as CornerDownRightIcon, Globe as GlobeIcon, RotateCcw as RotateCcwIcon,
  ThumbsDown as ThumbsDownIcon, ThumbsUp as ThumbsUpIcon,
} from 'lucide-react';
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion';
import { useEffect, useId, useMemo, useRef, useState } from 'react';
import { cn } from '../../lib/cn';
import { EASE_OUT } from '../../lib/ease';
import {
  COPIED_MILLISECONDS, DEFAULT_CHARACTERS_PER_SECOND, IDLE_REVEAL_CLOCK, defaultStreamingTextLabels, isPlainClick,
  nextFeedback, resolveLabels, revealFrame, sourceKeys, visibleLength as clampVisibleLength,
} from '../../lib/ai/streamingText';
import { buttonVariants } from '../ui/Button';

/*
 * Streaming Text (Honest UI port). Renders what it is given; there is no
 * timer inside. Append to `children` as the stream delivers and change
 * `status` when it ends or fails. Classes follow .local/honestui/TOKENS.md.
 */

/**
 * @typedef {'streaming' | 'complete' | 'error'} StreamingTextStatus
 * @typedef {'up' | 'down' | null} StreamingTextFeedback
 * @typedef {{
 *   title: string,
 *   href?: string,
 *   domain?: string,
 *   id?: string,
 *   onSelect?: (event: import('react').MouseEvent) => void,
 *   target?: string,
 *   rel?: string,
 * }} StreamingSource
 *   A web source (`href`), or an item that opens in the app (`onSelect`).
 *   With both, a plain click calls `onSelect` and a modified or middle click
 *   follows `href` (e.g. a GitHub permalink).
 */

// --hui-font-size-small (12/16) and --hui-transition-interactive, per TOKENS.md.
const smallText = 'text-xs';
// The global focus ring (index.css) replaces `outline-none … focus-visible:[outline:…]`.
const interactive = 'motion-safe:transition-interactive hover:bg-surface-2';
// A Markdown component draws the caret itself, at the end of its last line.
const contentStyles = 'prose-answer [&:has([data-slot=markdown])>[data-slot=streaming-text-caret]]:hidden';

/**
 * Paces received text at a steady rate: never runs ahead of what arrived,
 * catches up when the stream ends, and starts over when the text gets
 * shorter (a new reply). Under reduced motion (or `enabled: false`) it
 * returns the text as-is and `isSettled: true`.
 *
 * @param {string} text the text received so far
 * @param {{ charactersPerSecond?: number, enabled?: boolean }} [options]
 *   `enabled: false` turns pacing off (see `shouldPaceReply`); turn it off for
 *   good within a reply, it is not meant to switch back on.
 * @returns {{ text: string, isSettled: boolean }}
 */
function useSmoothStream(text, { charactersPerSecond = DEFAULT_CHARACTERS_PER_SECOND, enabled = true } = {}) {
  const shouldReduceMotion = useReducedMotion() ?? false;
  const isPaced = enabled && !shouldReduceMotion;
  const [shownLength, setShownLength] = useState(0);
  // The previous frame's time and sub-character progress, kept across runs of
  // the loop so a chunk that arrives mid-reveal does not slow it down.
  const clock = useRef(IDLE_REVEAL_CLOCK);
  // Text that got shorter is a new reply, so the reveal starts over from its length.
  if (shownLength > text.length) setShownLength(text.length);
  const visibleLength = clampVisibleLength(shownLength, text.length);
  const isBehind = visibleLength < text.length;

  useEffect(() => {
    if (!isPaced || !isBehind) return undefined;
    let frame = 0;
    let firstOfRun = true;
    let position = visibleLength;
    const step = (time) => {
      const next = revealFrame({ position, firstOfRun, clock: clock.current, time, length: text.length, charactersPerSecond });
      firstOfRun = false;
      position = next.position;
      clock.current = next.clock;
      setShownLength(next.shown);
      if (!next.done) frame = window.requestAnimationFrame(step);
    };
    frame = window.requestAnimationFrame(step);
    return () => window.cancelAnimationFrame(frame);
    // `visibleLength` is read once per run as the starting point.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [text, charactersPerSecond, isPaced, isBehind]);

  if (!isPaced) return { text, isSettled: true };
  return { text: text.slice(0, visibleLength), isSettled: !isBehind };
}

// Every reply has three or four of these, so the press is CSS: a spring per
// button meant a page of 30 older messages mounted a hundred motion values.
const PRESS = 'motion-safe:transition-[color,background-color,border-color,box-shadow,opacity,scale] motion-safe:duration-200 motion-safe:not-disabled:active:scale-90';

function ActionButton({ label, isPressed, className, children, onClick }) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      aria-pressed={isPressed}
      onClick={onClick}
      // The pressed fill is important so the link variant's hover cannot undo it.
      className={cn(buttonVariants({ variant: 'link', size: 'icon-sm' }), 'aria-pressed:bg-surface-3!', PRESS, className)}
    >
      {children}
    </button>
  );
}

function SourceLink({ source }) {
  const { href, onSelect, target, rel } = source;
  const className = cn('flex min-w-0 items-center gap-2 rounded-lg px-2 py-1 text-ink no-underline!', 'motion-safe:transition-interactive hover:bg-surface-3');
  const content = (
    <>
      <span className="min-w-0 truncate underline decoration-line-strong underline-offset-2">{source.title}</span>
      {source.domain && (
        <span className="ms-auto shrink-0 font-mono text-[11px] leading-4 text-ink-2">{source.domain}</span>
      )}
    </>
  );
  if (href) {
    return (
      <a
        href={href}
        target={target}
        rel={rel ?? (target === '_blank' ? 'noreferrer noopener' : undefined)}
        onClick={onSelect ? (event) => {
          if (!isPlainClick(event)) return;
          event.preventDefault();
          onSelect(event);
        } : undefined}
        className={className}
      >
        {content}
      </a>
    );
  }
  if (onSelect) {
    return (
      <button
        type="button"
        onClick={(event) => onSelect(event)}
        className={cn(className, 'w-full cursor-pointer border-0 bg-transparent text-start')}
      >
        {content}
      </button>
    );
  }
  return <span className="flex min-w-0 items-center gap-2 px-2 py-1 text-ink">{content}</span>;
}

/**
 * @param {{
 *   children?: import('react').ReactNode,
 *   status?: StreamingTextStatus,
 *   copyText?: string,
 *   onCopy?: () => void | Promise<void>,
 *   onRetry?: () => void,
 *   sources?: StreamingSource[],
 *   sourcesOpen?: boolean,
 *   defaultSourcesOpen?: boolean,
 *   onSourcesOpenChange?: (open: boolean) => void,
 *   sourcesIcon?: import('react').ComponentType<{ className?: string, 'aria-hidden'?: string }>,
 *   feedback?: StreamingTextFeedback,
 *   defaultFeedback?: StreamingTextFeedback,
 *   onFeedbackChange?: (feedback: StreamingTextFeedback) => void,
 *   announce?: boolean,
 *   showActions?: boolean,
 *   contentClassName?: string,
 *   labels?: Partial<typeof defaultStreamingTextLabels>,
 *   className?: string,
 * } & import('react').HTMLAttributes<HTMLDivElement>} props
 */
function StreamingText({
  children, status = 'complete', copyText, onCopy, onRetry, sources = [], sourcesOpen, defaultSourcesOpen = false,
  onSourcesOpenChange, sourcesIcon: SourcesIcon = GlobeIcon, feedback: feedbackProp, defaultFeedback = null,
  onFeedbackChange, announce = true, showActions = true, contentClassName, labels: labelsProp, className, ...props
}) {
  const labels = useMemo(() => resolveLabels(labelsProp), [labelsProp]);
  const [copyState, setCopyState] = useState(/** @type {'idle' | 'copied' | 'failed'} */ ('idle'));
  const [uncontrolledFeedback, setUncontrolledFeedback] = useState(defaultFeedback);
  const [uncontrolledSourcesOpen, setUncontrolledSourcesOpen] = useState(defaultSourcesOpen);
  const shouldReduceMotion = useReducedMotion() ?? false;
  const sourcesId = useId();
  const copyTimer = useRef(undefined);
  // The copy icon pops only once it has been used, not on every reply's mount.
  const copiedOnce = useRef(false);
  useEffect(() => () => window.clearTimeout(copyTimer.current), []);

  const isStreaming = status === 'streaming';
  const feedback = feedbackProp !== undefined ? feedbackProp : uncontrolledFeedback;
  const canCopy = copyText !== undefined || onCopy !== undefined;
  const canRate = status === 'complete' && (onFeedbackChange !== undefined || feedbackProp !== undefined);
  const hasSources = sources.length > 0;
  const isSourcesOpen = sourcesOpen ?? uncontrolledSourcesOpen;
  const hasActions = showActions && !isStreaming && (canCopy || onRetry !== undefined || canRate || hasSources);
  const keys = useMemo(() => sourceKeys(sources), [sources]);
  // Actions that appear when a reply finishes rise in; a reply that was
  // already finished (history) shows them in place.
  const [actionsEnter] = useState(() => !hasActions);

  const copy = async () => {
    copiedOnce.current = true;
    try {
      if (onCopy) await onCopy();
      else await navigator.clipboard.writeText(copyText ?? '');
      setCopyState('copied');
    } catch {
      setCopyState('failed');
    }
    window.clearTimeout(copyTimer.current);
    copyTimer.current = window.setTimeout(() => setCopyState('idle'), COPIED_MILLISECONDS);
  };
  const toggleSources = () => {
    if (sourcesOpen === undefined) setUncontrolledSourcesOpen(!isSourcesOpen);
    onSourcesOpenChange?.(!isSourcesOpen);
  };
  const rate = (value) => {
    const next = nextFeedback(feedback, value);
    if (feedbackProp === undefined) setUncontrolledFeedback(next);
    onFeedbackChange?.(next);
  };

  return (
    <div
      data-slot="streaming-text"
      data-status={status}
      aria-busy={isStreaming}
      className={cn('flex w-full min-w-0 flex-col gap-2', className)}
      {...props}
    >
      <div data-slot="streaming-text-content" aria-live={announce ? 'polite' : 'off'} className={cn(contentStyles, contentClassName)}>
        {children}
        {isStreaming && (
          <span
            aria-hidden="true"
            data-slot="streaming-text-caret"
            className="ms-0.5 inline-block h-[1em] w-0.5 translate-y-[0.15em] rounded-full bg-ink forced-colors:bg-[CanvasText]"
          />
        )}
      </div>
      {status === 'error' && (
        <p role="alert" className={cn('m-0 flex items-start gap-1', smallText)}>
          <CircleAlertIcon strokeWidth={1.5} strokeLinecap="round" aria-hidden="true" className="mt-px size-3.5 shrink-0 text-danger" />
          <span className="min-w-0 break-words">{labels.error}</span>
        </p>
      )}
      {hasActions && (
        <div
          data-slot="streaming-text-actions"
          className={cn(smallText, actionsEnter && 'animate-rise')}
        >
          <div className="flex min-w-0 flex-wrap items-center gap-0.5">
            {canCopy && (
              <ActionButton label={labels.copy} onClick={() => void copy()}>
                <span
                  key={copyState === 'copied' ? 'copied' : 'copy'}
                  aria-hidden="true"
                  className={cn('flex [&_svg]:size-4', copiedOnce.current && 'animate-pop')}
                >
                  {copyState === 'copied' ? <CheckIcon /> : <CopyIcon />}
                </span>
              </ActionButton>
            )}
            {onRetry && (
              <ActionButton label={labels.retry} onClick={onRetry}>
                <RotateCcwIcon aria-hidden="true" />
              </ActionButton>
            )}
            {canRate && (
              <>
                <ActionButton label={labels.helpful} isPressed={feedback === 'up'} onClick={() => rate('up')}>
                  <ThumbsUpIcon aria-hidden="true" />
                </ActionButton>
                <ActionButton label={labels.notHelpful} isPressed={feedback === 'down'} onClick={() => rate('down')}>
                  <ThumbsDownIcon aria-hidden="true" />
                </ActionButton>
              </>
            )}
            {hasSources && (
              <button
                type="button"
                data-slot="streaming-text-sources-trigger"
                aria-expanded={isSourcesOpen}
                aria-controls={sourcesId}
                onClick={toggleSources}
                className={cn('ms-0.5 inline-flex cursor-pointer items-center gap-1 rounded-lg border-0 bg-transparent px-1 py-1 text-ink-2', interactive)}
              >
                <SourcesIcon aria-hidden="true" className="size-3.5 shrink-0" />
                {labels.sources(sources.length)}
                <span
                  aria-hidden="true"
                  className={cn('flex motion-safe:transition-[rotate] motion-safe:duration-200 motion-safe:ease-(--ease-emphasized)', isSourcesOpen && 'rotate-180')}
                >
                  <ChevronDownIcon className="size-3.5 shrink-0" />
                </span>
              </button>
            )}
            {/* Mounted with the actions, so a copy is announced when it happens. */}
            <span role="status" className={copyState === 'failed' ? 'ms-1' : 'sr-only'}>
              {copyState === 'copied' ? labels.copied : copyState === 'failed' ? labels.copyFailed : ''}
            </span>
          </div>
          {hasSources && (
            <div id={sourcesId}>
              <AnimatePresence initial={false}>
                {isSourcesOpen && (
                  <motion.div
                    key="sources"
                    initial={shouldReduceMotion ? { opacity: 0 } : { height: 0, opacity: 0, y: -4 }}
                    animate={shouldReduceMotion ? { opacity: 1 } : { height: 'auto', opacity: 1, y: 0 }}
                    exit={shouldReduceMotion ? { opacity: 0 } : { height: 0, opacity: 0, y: -4 }}
                    transition={{ duration: shouldReduceMotion ? 0.12 : 0.22, ease: EASE_OUT }}
                    className="overflow-hidden"
                  >
                    <ul role="list" data-slot="streaming-text-sources" className="m-0 mt-1 flex list-none flex-col rounded-xl bg-surface-2 p-1">
                      {sources.map((source, index) => (
                        <motion.li
                          key={keys[index]}
                          initial={shouldReduceMotion ? false : { opacity: 0, y: 6 }}
                          animate={{ opacity: 1, y: 0 }}
                          transition={{ duration: 0.22, ease: EASE_OUT, delay: 0.04 * index }}
                          className="min-w-0"
                        >
                          <SourceLink source={source} />
                        </motion.li>
                      ))}
                    </ul>
                  </motion.div>
                )}
              </AnimatePresence>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * Inline source link: mono, small, tinted. Renders an `<a>` like the
 * reference; without `href` it renders a `<button type="button">` so an
 * in-app citation (onClick) stays focusable.
 *
 * @param {import('react').AnchorHTMLAttributes<HTMLAnchorElement> & import('react').ButtonHTMLAttributes<HTMLButtonElement>} props
 */
function StreamingCitation({ className, children, ...props }) {
  // `.prose-answer a` is unlayered, so the colours need `!` to beat it (as the
  // reference already does for the underline).
  const classes = cn(
    'mx-0.5 inline-flex items-center gap-0.5 rounded-md bg-surface-2 px-1 align-baseline font-mono text-ink-2! no-underline! text-[11px] leading-4 hover:text-ink!',
    'motion-safe:transition-interactive hover:bg-surface-3',
    className,
  );
  if (props.href === undefined) {
    return (
      <button type="button" data-slot="streaming-citation" className={cn(classes, 'cursor-pointer border-0')} {...props}>
        {children}
      </button>
    );
  }
  return <a data-slot="streaming-citation" className={classes} {...props}>{children}</a>;
}

/**
 * Follow-up prompts, shown once the reply has finished. Returns null for no prompts.
 *
 * @param {{ prompts: string[], onSelect: (prompt: string) => void, label?: string, className?: string } & import('react').HTMLAttributes<HTMLDivElement>} props
 */
function StreamingFollowUps({ prompts, onSelect, label = 'Follow-ups', className, ...props }) {
  const headingId = useId();
  if (!prompts || prompts.length === 0) return null;
  return (
    <div data-slot="streaming-follow-ups" className={cn('min-w-0', smallText, className)} {...props}>
      <p id={headingId} className="m-0 font-medium text-ink-2">{label}</p>
      <ul role="list" aria-labelledby={headingId} className="m-0 mt-1 flex list-none flex-col gap-0.5 p-0">
        {prompts.map((prompt) => (
          <li key={prompt}>
            <button
              type="button"
              onClick={() => onSelect(prompt)}
              className={cn('-mx-1 flex w-[calc(100%+0.5rem)] cursor-pointer items-center gap-2 rounded-lg border-0 bg-transparent px-1 py-1 text-left text-ink', interactive)}
            >
              <CornerDownRightIcon aria-hidden="true" className="size-3.5 shrink-0 text-ink-3" />
              <span className="min-w-0 break-words">{prompt}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

export { StreamingText, StreamingCitation, StreamingFollowUps, useSmoothStream, defaultStreamingTextLabels };
