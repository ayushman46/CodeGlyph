import { animate, motion, useMotionValue, useReducedMotion, useTransform } from 'framer-motion';
import { TriangleAlert as TriangleAlertIcon } from 'lucide-react';
import { Fragment, useEffect, useId } from 'react';
import { cn } from '../../lib/cn';
import { EASE_OUT } from '../../lib/ease';
import {
  CHANGE_SECONDS, DEFAULT_NEAR_AT, RING_RADIUS, barWidthFor, clampUsed, defaultContextMeterLabels, meterRatio,
  meterStatus, remainingOf, resolveLabels, ringDashFor, statusLabelFor, toPercent,
} from '../../lib/ai/contextMeter';
import { Popover, PopoverPopup, PopoverTrigger } from '../ui/Popover';

/*
 * Context Meter (Honest UI port): a small ring + percentage that opens a
 * details popover with a meter bar, the counts, the status and an optional
 * breakdown. It counts nothing itself: pass `used` and `limit` (and `parts`)
 * from the API, and adapt the wording/units with `labels` (presets:
 * `bytesLabels` for indexed source bytes vs the quota, `filesLabels` for
 * files vs a file budget). Sized to sit in a row of controls (a PromptBar's
 * `actions`). It does not announce threshold crossings (not a live region).
 */

const warningStroke = { strokeWidth: 1.5, strokeLinecap: 'round' };

const statusTone = {
  ok: 'text-ink',
  near: 'text-ink',
  // The darker danger step, readable as small text.
  full: 'text-danger-strong',
};

/**
 * @typedef {import('../../lib/ai/contextMeter').ContextMeterStatus} ContextMeterStatus
 * @typedef {import('../../lib/ai/contextMeter').ContextMeterPart} ContextMeterPart
 * @typedef {import('../../lib/ai/contextMeter').ContextMeterLabels} ContextMeterLabels
 */

/**
 * @param {object} props
 * @param {number} props.used Count used so far (tokens by default; bytes with `bytesLabels`). Below 0 reads 0.
 * @param {number} props.limit The limit; 0 or less reads 0%.
 * @param {ContextMeterPart[]} [props.parts=[]] Breakdown rows, label → count (formatted by `labels.number`).
 * @param {import('react').ReactNode} [props.cost] Cost row value, shown as given.
 * @param {number} [props.nearAt=0.8] Share of the limit from which the meter reads "near".
 * @param {boolean} [props.showPercent=true] Show the percentage next to the ring.
 * @param {import('react').ReactNode} [props.note] A line under the details.
 * @param {Partial<ContextMeterLabels>} [props.labels] Every string, merged over `defaultContextMeterLabels`.
 * @param {string} [props.className] Extra classes for the trigger button.
 * Any other prop goes to the trigger button.
 */
function ContextMeter({
  used, limit, parts = [], cost, nearAt = DEFAULT_NEAR_AT, showPercent = true, note, labels: labelsProp, className, ...props
}) {
  const labels = resolveLabels(labelsProp);
  const titleId = useId();
  const usedTokens = clampUsed(used);
  const ratio = meterRatio(usedTokens, limit);
  const percent = toPercent(ratio);
  const status = meterStatus(ratio, nearAt);
  const statusLabel = statusLabelFor(status, labels);
  const shouldReduceMotion = useReducedMotion() ?? false;

  // The ring, percentage and bar glide from the old figure to the new one;
  // status and the accessible name change at once. Nothing glides on first
  // appearance (the value starts at the current ratio).
  const shownRatio = useMotionValue(ratio);
  useEffect(() => {
    if (shouldReduceMotion) {
      shownRatio.set(ratio);
      return undefined;
    }
    const controls = animate(shownRatio, ratio, { duration: CHANGE_SECONDS, ease: EASE_OUT });
    return () => controls.stop();
  }, [ratio, shouldReduceMotion, shownRatio]);

  const ringDash = useTransform(shownRatio, ringDashFor);
  const shownPercent = useTransform(shownRatio, (value) => `${toPercent(value)}%`);
  const barWidth = useTransform(shownRatio, barWidthFor);
  const tokensText = labels.tokens(labels.number(usedTokens), labels.number(limit));

  return (
    <Popover>
      <PopoverTrigger
        data-slot="context-meter"
        data-status={status}
        className={cn(
          'inline-flex shrink-0 cursor-pointer items-center gap-1 rounded-lg border-0 bg-transparent px-1 py-1 text-[11px] leading-3.5 tabular-nums motion-safe:transition-interactive hover:bg-surface-2 data-popup-open:bg-surface-2',
          statusTone[status],
          className,
        )}
        {...props}
      >
        <svg aria-hidden="true" viewBox="0 0 16 16" className="size-4 shrink-0 -rotate-90">
          <circle cx="8" cy="8" r={RING_RADIUS} fill="none" stroke="currentColor" strokeWidth="2" opacity="0.2" />
          <motion.circle
            cx="8"
            cy="8"
            r={RING_RADIUS}
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            style={{ strokeDasharray: ringDash }}
          />
        </svg>
        {showPercent && (
          <motion.span aria-hidden="true" data-slot="context-meter-percent">{shownPercent}</motion.span>
        )}
        {statusLabel && <TriangleAlertIcon {...warningStroke} aria-hidden="true" className="size-3.5 shrink-0" />}
        <span className="sr-only">
          {labels.summary(percent)}
          {statusLabel && `. ${statusLabel}`}
        </span>
      </PopoverTrigger>
      <PopoverPopup align="end" aria-labelledby={titleId} className="w-64 max-w-[calc(100vw-2rem)]">
        <div data-slot="context-meter-details" className="flex flex-col gap-2">
          <p id={titleId} className="m-0 font-medium">{labels.title}</p>
          <div className="flex flex-col gap-1">
            <div
              role="meter"
              aria-labelledby={titleId}
              aria-valuemin={0}
              aria-valuemax={limit}
              aria-valuenow={Math.min(usedTokens, limit)}
              aria-valuetext={tokensText}
              className="h-1.5 w-full overflow-hidden rounded-full bg-surface-2"
            >
              {/* A border, not a fill, so the bar survives forced colors. */}
              <motion.div
                className={cn('h-full rounded-full border-t-[6px] border-current', statusTone[status])}
                style={{ width: barWidth }}
              />
            </div>
            <p className="m-0 flex flex-wrap items-baseline justify-between gap-x-2">
              <span>{tokensText}</span>
              <span className="text-ink-2">{labels.remaining(labels.number(remainingOf(usedTokens, limit)))}</span>
            </p>
            {statusLabel && (
              <p className={cn('m-0 flex items-center gap-1', statusTone[status])}>
                <TriangleAlertIcon {...warningStroke} aria-hidden="true" className="size-3.5 shrink-0" />
                <span className="font-medium">{statusLabel}</span>
              </p>
            )}
          </div>
          {(parts.length > 0 || cost !== undefined) && (
            <dl className="m-0 grid grid-cols-[minmax(0,1fr)_auto] gap-x-3 gap-y-0.5 border-t border-line pt-2">
              {parts.map((part) => (
                <Fragment key={part.label}>
                  <dt className="min-w-0 break-words text-ink-2">{part.label}</dt>
                  <dd className="m-0 text-end tabular-nums">{labels.number(part.tokens)}</dd>
                </Fragment>
              ))}
              {cost !== undefined && (
                <>
                  <dt className="text-ink-2">{labels.cost}</dt>
                  <dd className="m-0 text-end tabular-nums">{cost}</dd>
                </>
              )}
            </dl>
          )}
          {note && <p className="m-0 break-words text-ink-2">{note}</p>}
        </div>
      </PopoverPopup>
    </Popover>
  );
}

export { ContextMeter, defaultContextMeterLabels };
export { bytesLabels, filesLabels } from '../../lib/ai/contextMeter';
export default ContextMeter;
