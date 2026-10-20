import { ChevronDown as ChevronDownIcon, CircleAlert as CircleAlertIcon, LoaderCircle as LoaderCircleIcon, Wrench as WrenchIcon } from 'lucide-react';
import { useLayoutEffect, useMemo, useRef, useState } from 'react';
import { cn } from '../../lib/cn';
import { defaultToolChipsLabels } from '../../lib/ai/toolChips';
import { Collapsible, CollapsiblePanel, CollapsibleTrigger } from '../ui/Collapsible';
import { Popover, PopoverPopup, PopoverTrigger } from '../ui/Popover';

/*
 * Tool Chips (Honest UI port): a summary button that opens the list of an
 * agent run's tool calls, then one chip per changed file with its +added /
 * −removed counts and the diff in a popover (hover after 150ms, or press /
 * keyboard; Escape closes it). Data mappers for the code editor's
 * `tool_calls` and proposal files live in lib/ai/toolChips.js.
 *
 * It does not announce new calls as they arrive.
 */

/**
 * @typedef {import('../../lib/ai/toolChips').ToolLine} ToolLine
 * @typedef {import('../../lib/ai/toolChips').ToolCall} ToolCall
 * @typedef {import('../../lib/ai/toolChips').FileChange} FileChange
 * @typedef {typeof defaultToolChipsLabels} ToolChipsLabels
 */

export { defaultToolChipsLabels };

const rowText = 'text-xs';
const codeText = 'font-mono text-[11px] leading-4';
const interactiveRow = 'border-0 bg-transparent motion-safe:transition-interactive hover:bg-surface-2';
const rowEnter = 'motion-safe:transition-[opacity,translate] motion-safe:duration-300 motion-safe:starting:translate-y-1 motion-safe:starting:opacity-0';
const lineTone = {
  context: 'text-ink-2',
  added: 'bg-success-soft text-ink',
  removed: 'bg-danger-soft text-ink',
};
const addedCountTone = 'text-success';
const removedCountTone = 'text-danger-strong';
const lineSign = { context: ' ', added: '+', removed: '−' };

/**
 * @param {object} props
 * @param {import('react').ReactNode} props.summary the one-line headline of the run (the toggle's text)
 * @param {ToolCall[]} [props.calls=[]]
 * @param {FileChange[]} [props.changes=[]]
 * @param {number} [props.maxVisibleChanges=3] file chips shown before "Show N more"
 * @param {boolean} [props.open] controlled open state of the list
 * @param {boolean} [props.defaultOpen=true]
 * @param {(open: boolean) => void} [props.onOpenChange]
 * @param {Partial<ToolChipsLabels>} [props.labels]
 * @param {string} [props.className]
 */
function ToolChips({
  summary, calls = [], changes = [], maxVisibleChanges = 3, open, defaultOpen = true, onOpenChange,
  labels: labelsProp, className, ...props
}) {
  const labels = useMemo(() => ({ ...defaultToolChipsLabels, ...labelsProp }), [labelsProp]);
  const [showAllChanges, setShowAllChanges] = useState(false);
  const visibleChanges = showAllChanges ? changes : changes.slice(0, maxVisibleChanges);
  const hiddenCount = changes.length - visibleChanges.length;
  const changesRef = useRef(/** @type {HTMLDivElement | null} */ (null));
  const revealFocusRef = useRef(false);

  // "Show N more" unmounts as it reveals the rest: focus moves to the first
  // revealed chip (or the group, when that chip has no diff to open), so it
  // does not fall back to the page.
  useLayoutEffect(() => {
    if (!showAllChanges || !revealFocusRef.current) return;
    revealFocusRef.current = false;
    const group = changesRef.current;
    const first = group?.querySelectorAll('[data-slot="tool-chips-change"]')[maxVisibleChanges];
    if (first instanceof HTMLElement && first.tagName === 'BUTTON') first.focus();
    else group?.focus();
  }, [showAllChanges, maxVisibleChanges]);

  return (
    <div data-slot="tool-chips" className={cn('flex w-full min-w-0 flex-col text-ink', rowText, className)} {...props}>
      <Collapsible open={open} defaultOpen={defaultOpen} onOpenChange={onOpenChange}>
        <CollapsibleTrigger
          data-slot="tool-chips-trigger"
          className={cn('group/summary -mx-1 flex w-fit max-w-full items-center gap-1 rounded-lg px-1 py-1 text-left text-ink-2', interactiveRow)}
        >
          <ChevronDownIcon aria-hidden="true" className="size-3.5 shrink-0 -rotate-90 motion-safe:transition-transform motion-safe:duration-200 group-data-panel-open/summary:rotate-0" />
          <span className="min-w-0 truncate">{summary}</span>
        </CollapsibleTrigger>
        <CollapsiblePanel>
          <ul role="list" data-slot="tool-chips-calls" className="m-0 flex list-none flex-col gap-0.5 p-0 pt-1">
            {calls.map((call) => (<ToolCallRow key={call.id} call={call} labels={labels} />))}
          </ul>
          {changes.length > 0 && (
            <div
              ref={changesRef}
              role="group"
              aria-label={labels.changes}
              tabIndex={showAllChanges ? -1 : undefined}
              data-slot="tool-chips-changes"
              className="mt-2 flex min-w-0 flex-wrap items-center gap-1 border-t border-line pt-2"
            >
              {visibleChanges.map((change) => (<FileChangeChip key={change.path} change={change} labels={labels} />))}
              {hiddenCount > 0 && (
                <button
                  type="button"
                  onClick={(event) => {
                    // Only when the button had focus (Safari does not focus a clicked button).
                    revealFocusRef.current = event.currentTarget === document.activeElement;
                    setShowAllChanges(true);
                  }}
                  className={cn('rounded-lg px-1 py-1 text-ink-2 underline underline-offset-2', interactiveRow)}
                >
                  {labels.showMoreChanges(hiddenCount)}
                </button>
              )}
            </div>
          )}
        </CollapsiblePanel>
      </Collapsible>
    </div>
  );
}

/** @param {{ call: ToolCall, labels: ToolChipsLabels }} props */
function ToolCallRow({ call, labels }) {
  const status = call.status ?? 'done';
  const hasOutput = Boolean(call.output?.length);
  const summary = (
    <>
      <span
        aria-hidden="true"
        className={cn('flex size-4 shrink-0 items-center justify-center [&_svg]:size-3.5', status === 'error' ? 'text-danger' : 'text-ink-3')}
      >
        {status === 'running' ? (
          <LoaderCircleIcon className="motion-safe:animate-spin" />
        ) : status === 'error' ? (
          <CircleAlertIcon strokeWidth={1.5} strokeLinecap="round" />
        ) : (call.icon ?? <WrenchIcon />)}
      </span>
      <span className="shrink-0 font-medium">
        {call.label}
        {status !== 'done' && (<span className="sr-only"> ({status === 'running' ? labels.running : labels.failed})</span>)}
      </span>
      {call.detail && (
        <span className={cn('min-w-0 truncate rounded-lg bg-surface-2 px-1 py-0.5 text-ink-2', call.detailIsCode && codeText)}>
          {call.detail}
        </span>
      )}
    </>
  );
  const rowLayout = 'flex w-full min-w-0 items-center gap-2 rounded-lg px-1 py-1 text-left';

  if (!hasOutput) {
    return (<li data-slot="tool-call" data-status={status} className={cn(rowLayout, rowEnter)}>{summary}</li>);
  }
  return (
    <li data-slot="tool-call" data-status={status} className={cn('min-w-0', rowEnter)}>
      <Collapsible>
        <CollapsibleTrigger className={cn('group/call', rowLayout, interactiveRow)}>
          {summary}
          <ChevronDownIcon aria-hidden="true" className="ms-auto size-3.5 shrink-0 -rotate-90 text-ink-3 motion-safe:transition-transform motion-safe:duration-200 group-data-panel-open/call:rotate-0" />
        </CollapsibleTrigger>
        <CollapsiblePanel>
          <ToolLines lines={call.output ?? []} isCode={call.outputIsCode} className="ms-3 mt-0.5 mb-1 border-s border-line ps-2" />
        </CollapsiblePanel>
      </Collapsible>
    </li>
  );
}

/**
 * @param {{ lines: ToolLine[], isCode?: boolean, scroll?: boolean, className?: string }} props
 * `scroll={false}` leaves sideways scrolling to a container that can take
 * focus (the diff popover), so the keyboard can scroll long lines.
 */
function ToolLines({ lines, isCode = false, scroll = true, className }) {
  const hasChanges = lines.some((line) => line.kind === 'added' || line.kind === 'removed');
  return (
    <div
      className={cn(
        'min-w-0',
        scroll && 'overflow-x-auto',
        // Code scrolls sideways as one block: every line is as wide as the
        // longest, so the tints run under the whole line while scrolled.
        isCode ? ['grid [grid-template-columns:minmax(max-content,1fr)] [tab-size:2]', codeText] : 'flex flex-col',
        className,
      )}
    >
      {lines.map((line, index) => {
        const kind = line.kind ?? 'context';
        return (
          <div key={index} className={cn('flex gap-2 px-1 leading-5', isCode ? 'whitespace-pre' : 'break-words', lineTone[kind])}>
            {/* The sign carries the meaning, so the tint is never the only cue. */}
            {hasChanges && <span className="w-[1ch] shrink-0 select-none">{lineSign[kind]}</span>}
            <span className="min-w-0">{line.text}</span>
          </div>
        );
      })}
    </div>
  );
}

/** @param {{ change: FileChange, labels: ToolChipsLabels }} props */
function FileChangeChip({ change, labels }) {
  const chipLayout = cn('inline-flex max-w-full items-center gap-2 rounded-lg border border-line-strong bg-surface px-2 py-1 text-ink', codeText);
  const counts = (
    <>
      <span className="min-w-0 truncate">{change.path}</span>
      <span className={cn('shrink-0 tabular-nums', addedCountTone)}>
        <span aria-hidden="true">+{change.added}</span>
        <span className="sr-only">{labels.added(change.added)}</span>
      </span>
      {change.removed > 0 && (
        <span className={cn('shrink-0 tabular-nums', removedCountTone)}>
          <span aria-hidden="true">−{change.removed}</span>
          <span className="sr-only">{labels.removed(change.removed)}</span>
        </span>
      )}
    </>
  );

  if (!change.lines?.length) return (<span data-slot="tool-chips-change" className={chipLayout}>{counts}</span>);
  return (
    <Popover>
      <PopoverTrigger
        openOnHover
        delay={150}
        data-slot="tool-chips-change"
        className={cn(chipLayout, 'cursor-pointer motion-safe:transition-interactive hover:bg-surface-2 data-popup-open:bg-surface-2')}
      >
        {counts}
      </PopoverTrigger>
      {/* The popup (which takes focus) scrolls both ways, so the arrow keys
          reach the end of long lines; the lines do not scroll on their own. */}
      <PopoverPopup align="start" aria-label={labels.diffFor(change.path)} className="w-80 max-w-[calc(100vw-2rem)] overflow-hidden p-0">
        {/* The scrolling region takes focus, so a long diff can be scrolled by keyboard. */}
        <div
          tabIndex={0}
          role="group"
          aria-label={labels.diffFor(change.path)}
          className="max-h-[min(20rem,var(--available-height,20rem))] overflow-auto outline-none focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent"
        >
          <ToolLines lines={change.lines} isCode scroll={false} className="py-1" />
        </div>
      </PopoverPopup>
    </Popover>
  );
}

export { ToolChips };
export default ToolChips;
