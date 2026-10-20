import { forwardRef, useEffect, useState } from 'react';
import { cn } from '../../lib/cn';
import {
  formatElapsed, loadingCellStyle, resolveLoadingPattern, resolveStartTime,
} from '../../lib/ai/loadingState';
import { TextShimmer } from '../ui/TextShimmer';

/** @typedef {import('../../lib/ai/loadingState').LoadingPattern} LoadingPattern */

/**
 * Seconds since `startedAt` (or mount), updated once a second while enabled.
 *
 * @param {number|Date|string|null|undefined} startedAt
 * @param {boolean} isEnabled
 */
function useElapsed(startedAt, isEnabled) {
  const [mountedAt] = useState(() => Date.now());
  const start = resolveStartTime(startedAt, mountedAt);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!isEnabled) return undefined;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [isEnabled]);
  return formatElapsed(now - start);
}

const hasContent = (node) => node !== undefined && node !== null && node !== false && node !== '';

/**
 * One line that says work is in progress (Honest UI Loading State): a small
 * decorative animated mark, the label (a polite `role="status"`, shimmering)
 * and the elapsed time (`42s`, then `1m 12s`; aria-hidden so a ticking timer
 * is never announced). Write the label as the work ("Chunking 6,398
 * files"), not "Loading", and keep counts that move on every update in
 * `details`: each change to the label is announced.
 *
 * Optional live `details` (or children) render as a small secondary line
 * under the label. They are deliberately outside the status region, so
 * counts that change on every poll are readable but not announced; without
 * them the component is the reference's single line.
 *
 * Reduced motion: the cells stay dim and the label stops shimmering (the
 * elapsed time still updates). Forced colors: cells draw in CanvasText.
 * Styles: `.ci-loading-cell` and its keyframes in index.css.
 *
 * @typedef {object} LoadingStateProps
 * @property {import('react').ReactNode} label  what is in progress (the announced status)
 * @property {LoadingPattern} [pattern='drive']  drive | dots | orbit | diagonal | diamond | square | pulse
 * @property {boolean} [showElapsed=true]  show the elapsed time
 * @property {number|Date|string|null} [startedAt]  start of the work (epoch ms, Date or ISO string); defaults to mount
 * @property {import('react').ReactNode} [details]  live secondary line under the label (not announced)
 * @property {import('react').ReactNode} [children]  used as `details` when `details` is not passed
 * @property {string} [className]
 *
 * @type {import('react').ForwardRefExoticComponent<LoadingStateProps & Omit<import('react').HTMLAttributes<HTMLDivElement>, 'children'> & import('react').RefAttributes<HTMLDivElement>>}
 */
export const LoadingState = forwardRef(function LoadingState({
  label, pattern = 'drive', showElapsed = true, startedAt, details, children, className, ...props
}, ref) {
  const elapsed = useElapsed(startedAt, showElapsed);
  const resolved = resolveLoadingPattern(pattern);
  const { delays, isRound } = resolved;
  const detailContent = hasContent(details) ? details : children;
  const withDetails = hasContent(detailContent);

  const line = (
    <>
      <span
        aria-hidden="true"
        data-slot="loading-state-mark"
        data-pattern={resolved.name}
        className="grid size-[15px] shrink-0 grid-cols-[repeat(3,4px)] content-center gap-[1.5px]"
      >
        {delays.map((delay, index) => (
          <span
            key={index}
            className={cn('ci-loading-cell size-[4px] bg-ink', isRound ? 'rounded-full' : 'rounded-[1px]')}
            style={loadingCellStyle(delay, resolved)}
          />
        ))}
      </span>
      {/* Only the label is a status. A ticking timer would be announced every second. */}
      <span role="status" className="min-w-0">
        <TextShimmer duration={1.6} className="font-medium">{label}</TextShimmer>
      </span>
      {showElapsed && (
        <span
          aria-hidden="true"
          suppressHydrationWarning
          data-slot="loading-state-elapsed"
          className="shrink-0 font-mono text-[11px] leading-4 tabular-nums text-ink-2"
        >
          {elapsed}
        </span>
      )}
    </>
  );

  if (!withDetails) {
    return (
      <div
        ref={ref}
        data-slot="loading-state"
        className={cn('flex w-fit max-w-full items-center gap-2 text-xs', className)}
        {...props}
      >
        {line}
      </div>
    );
  }

  return (
    <div
      ref={ref}
      data-slot="loading-state"
      className={cn('flex w-fit max-w-full flex-col gap-0.5 text-xs', className)}
      {...props}
    >
      <div data-slot="loading-state-line" className="flex max-w-full items-center gap-2">
        {line}
      </div>
      {/* Indented to the label: the 15px mark plus the 8px gap. */}
      <div
        data-slot="loading-state-details"
        className="min-w-0 ps-[calc(15px+0.5rem)] text-[11px] leading-3.5 break-words tabular-nums text-ink-2"
      >
        {detailContent}
      </div>
    </div>
  );
});

LoadingState.displayName = 'LoadingState';

export default LoadingState;
