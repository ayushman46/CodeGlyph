import { AnimatePresence, motion, useReducedMotion } from 'framer-motion';
import {
  forwardRef, useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState,
} from 'react';
import {
  DEFAULT_FOLLOW_THRESHOLD, OPENING_SETTLE_MS, RAIL_INSET, SMOOTH_SCROLL_POLL_MS, SMOOTH_SCROLL_SETTLE_MS, centeredScrollTop,
  describeRailMessages, distanceFromEnd, fitRailItemSize, isLeaveLiveEdgeKey, isRailOverflowing, normalizeMessageText,
  pickActiveRailId, railItemsEqual, smoothScrollSettled, tickScale,
} from '../../lib/ai/messageScroller';
import { cn } from '../../lib/cn';
import { EASE_OUT, SPRING_LAYOUT } from '../../lib/ease';
import { isHoveringPointer } from '../ui/Popover';

/*
 * Message Scroller (Honest UI port, .local/honestui/02-message-scroller.md).
 *
 * A transcript viewport that keeps new output in view while the reader stays
 * near the end, with an optional rail of message ticks (`navigation="rail"`)
 * that previews and jumps to each message.
 *
 * Class defaults that a caller may override are written with the
 * `[:where(&)]:` variant, which gives them zero specificity: the reference
 * resolves "caller class beats default" with tailwind-merge, while this
 * app's cn() only joins, so a plain default could otherwise win on
 * Tailwind's own utility order (`relative` beats `absolute`, `w-12` beats
 * `w-7`).
 */

export { isHoveringPointer };

/* ------------------------------------------------------------------ */
/* Dismiss and pointer gestures                                        */
/* ------------------------------------------------------------------ */

/**
 * @typedef {'pass-through' | 'consume'} DismissBehavior
 * @typedef {object} DismissOptions
 * @property {DismissBehavior} [behavior] `consume` swallows the click that
 *   follows a dismissing press (unless another open scope claims it).
 * @property {boolean} [escape] Dismiss on Escape (default true).
 * @property {(target: Element) => boolean} [ignore] Targets that count as inside.
 */

/** @type {Set<(target: Element) => boolean>} */
const openScopes = new Set();

function claimedByAnotherScope(self, target) {
  for (const scope of openScopes) {
    if (scope !== self && scope(target)) return true;
  }
  return false;
}

function consumeActivation(source) {
  const swallow = (event) => {
    event.preventDefault();
    event.stopPropagation();
    release();
  };
  const restart = (event) => {
    if (event !== source) release();
  };
  const release = () => {
    window.removeEventListener('click', swallow, true);
    window.removeEventListener('pointerdown', restart, true);
    window.removeEventListener('pointercancel', restart, true);
    window.removeEventListener('keydown', release, true);
  };
  window.addEventListener('click', swallow, true);
  window.addEventListener('pointerdown', restart, true);
  window.addEventListener('pointercancel', restart, true);
  window.addEventListener('keydown', release, true);
}

/**
 * Calls `onDismiss` on Escape or on a press outside `ref` while `open`.
 * Keep `onDismiss` and `ignore` stable, for example with useCallback.
 *
 * @param {boolean} open
 * @param {() => void} onDismiss
 * @param {{ current: Element | null } | null} ref
 * @param {DismissOptions} [options]
 */
export function useDismiss(open, onDismiss, ref, { behavior = 'pass-through', escape: dismissOnEscape = true, ignore } = {}) {
  useEffect(() => {
    if (!open) return undefined;
    const inside = (target) => Boolean(ref?.current?.contains(target)) || Boolean(ignore?.(target));
    const onKey = (event) => {
      if (dismissOnEscape && event.key === 'Escape') onDismiss();
    };
    const onPointer = (event) => {
      const { target } = event;
      if (!(target instanceof Element) || inside(target)) return;
      if (behavior === 'consume' && !claimedByAnotherScope(inside, target)) consumeActivation(event);
      onDismiss();
    };
    openScopes.add(inside);
    window.addEventListener('keydown', onKey);
    window.addEventListener('pointerdown', onPointer, true);
    return () => {
      openScopes.delete(inside);
      window.removeEventListener('keydown', onKey);
      window.removeEventListener('pointerdown', onPointer, true);
    };
  }, [open, onDismiss, ref, behavior, dismissOnEscape, ignore]);
}

/**
 * Pairs each pointer leave with its enter, so pen contact cannot tear down
 * a tap. `enter` returns true for a hovering pointer; `leave` returns true
 * when the leave should end the hover.
 *
 * @returns {{ enter: (event: PointerEvent | React.PointerEvent) => boolean, leave: (event: PointerEvent | React.PointerEvent) => boolean }}
 */
export function useHoverGesture() {
  const contact = useRef(new Set());
  return useMemo(() => ({
    enter: (event) => {
      if (isHoveringPointer(event)) {
        contact.current.delete(event.pointerId);
        return true;
      }
      contact.current.add(event.pointerId);
      return false;
    },
    leave: (event) => {
      const arrivedInContact = contact.current.delete(event.pointerId);
      return !arrivedInContact && event.pointerType !== 'touch';
    },
  }), []);
}

/**
 * Remembers the pointer that started a press until the click reads it.
 * Wire `drop` to pointercancel and keydown so keyboard clicks cannot
 * inherit a tap.
 *
 * @template S
 * @returns {{
 *   start: (event: { pointerType: string }, state: S) => void,
 *   take: () => ({ pointerType: string, state: S } | null),
 *   drop: () => void,
 * }}
 */
export function useTapGesture() {
  const record = useRef(null);
  return useMemo(() => ({
    start: (event, state) => {
      record.current = { pointerType: event.pointerType, state };
    },
    take: () => {
      const spent = record.current;
      record.current = null;
      return spent;
    },
    drop: () => {
      record.current = null;
    },
  }), []);
}

/* ------------------------------------------------------------------ */
/* PreviewRail                                                         */
/* ------------------------------------------------------------------ */

/**
 * @typedef {object} PreviewRailItem
 * @property {string} id
 * @property {string} label Preview title; also the accessible name unless `ariaLabel` is set.
 * @property {string} [ariaLabel]
 * @property {React.ReactNode} [description]
 * @property {string} [href] Renders the item as a link (a tap first pins its preview).
 * @property {'_blank' | '_self' | '_parent' | '_top'} [target]
 * @property {string} [rel]
 */

/**
 * @typedef {object} PreviewRailProps
 * @property {PreviewRailItem[]} items
 * @property {string} [label] Navigation landmark name (default "Section navigation").
 * @property {'vertical' | 'horizontal'} [orientation] Default "vertical".
 * @property {string} [activeId] Controlled selected item.
 * @property {string} [defaultActiveId] Uncontrolled initial selection (default: the first item).
 * @property {(id: string) => void} [onActiveChange]
 * @property {(item: PreviewRailItem) => void} [onItemSelect]
 * @property {(item: PreviewRailItem) => React.ReactNode} [renderPreview] Replaces the default card.
 * @property {boolean} [showPreview] Default true.
 * @property {'before' | 'after'} [previewSide] Vertical rails only; default "after".
 * @property {boolean} [highlightActive] Highlight the selected tick when nothing is hovered or focused.
 * @property {number} [itemSize] Row (or column) size in px, default 24.
 * @property {React.ReactNode} [children] Content laid out beside the rail.
 * @property {string} [className]
 * @property {string} [railClassName]
 * @property {string} [previewContainerClassName]
 * @property {string} [previewClassName]
 */

/** @param {{ item: PreviewRailItem }} props */
function DefaultPreview({ item }) {
  return (
    <div data-slot="preview-rail-card" className="rounded-2xl border border-line bg-surface p-4 shadow-[var(--shadow-soft)] dark:border-line-strong">
      <p data-slot="preview-rail-title" className="font-medium text-ink">{item.label}</p>
      {item.description ? (
        <div data-slot="preview-rail-description" className="mt-1 text-sm leading-6 text-ink-2">{item.description}</div>
      ) : null}
    </div>
  );
}

/** Arrow keys that move focus along a rail, by orientation. */
const RAIL_STEP_KEYS = {
  vertical: { ArrowDown: 1, ArrowUp: -1 },
  horizontal: { ArrowRight: 1, ArrowLeft: -1 },
};

/**
 * A rail of ticks, one per item. Hovering or keyboard-focusing a tick
 * previews its item; a touch pins the preview until a tap outside, Escape or
 * the rail losing focus; activating a tick selects it.
 *
 * Keyboard (a deviation from the reference, where every tick is a tab stop):
 * the rail is one tab stop, on the selected tick; the arrow keys along the
 * rail and Home / End move between ticks. A rail longer than its box
 * scrolls (give the <nav> an overflow through `railClassName`); the preview
 * follows that scroll.
 *
 * @param {PreviewRailProps} props
 */
export function PreviewRail({
  items,
  label = 'Section navigation',
  orientation = 'vertical',
  activeId,
  defaultActiveId,
  onActiveChange,
  onItemSelect,
  renderPreview,
  showPreview = true,
  previewSide = 'after',
  highlightActive = false,
  itemSize = 24,
  children,
  className,
  railClassName,
  previewContainerClassName,
  previewClassName,
}) {
  const uid = useId();
  const reduce = useReducedMotion();
  const railRef = useRef(null);
  const [internalActiveId, setInternalActiveId] = useState(defaultActiveId ?? items[0]?.id ?? '');
  const [hoveredId, setHoveredId] = useState(null);
  // A finger cannot hover, so a tap lights the tick instead. Kept apart from the hovered one.
  const [pinnedId, setPinnedId] = useState(null);
  const [focusedId, setFocusedId] = useState(null);
  // The tick holding the rail's one tab stop while focus is in the rail
  // (null: the selected tick holds it).
  const [rovingId, setRovingId] = useState(null);
  // How far a rail longer than its box is scrolled; the preview follows it.
  const [railScroll, setRailScroll] = useState(0);
  const tap = useTapGesture();
  const hover = useHoverGesture();
  const clearPinned = useCallback(() => setPinnedId(null), []);
  // The next tap outside the rail stands in for the pointer leaving it.
  // Measured against the <nav>, not the whole root: the root also holds
  // `children` (the MessageScroller transcript), and a tap there must clear
  // the pin too.
  useDismiss(pinnedId !== null, clearPinned, railRef);

  const requestedActiveId = activeId ?? internalActiveId;
  const selectedId = items.some((item) => item.id === requestedActiveId) ? requestedActiveId : (items[0]?.id ?? '');
  const tabStopId = rovingId !== null && items.some((item) => item.id === rovingId) ? rovingId : selectedId;
  const displayedId = hoveredId ?? pinnedId ?? focusedId ?? '';
  const highlightedId = displayedId || (highlightActive ? selectedId : '');
  const displayedIndex = items.findIndex((item) => item.id === highlightedId);
  const rowTemplate = items.length ? `repeat(${items.length}, ${itemSize}px)` : undefined;
  const isHorizontal = orientation === 'horizontal';
  const selectItem = (id) => {
    if (activeId === undefined) setInternalActiveId(id);
    onActiveChange?.(id);
  };
  const stepKeys = RAIL_STEP_KEYS[isHorizontal ? 'horizontal' : 'vertical'];
  /** @param {import('react').KeyboardEvent<HTMLElement>} event */
  const moveFocus = (event) => {
    const ticks = Array.from(railRef.current?.querySelectorAll('[data-slot="preview-rail-item"]') ?? []);
    const current = ticks.indexOf(event.target instanceof Element ? event.target.closest('[data-slot="preview-rail-item"]') : null);
    if (current < 0 || event.altKey || event.ctrlKey || event.metaKey) return;
    let next;
    if (event.key in stepKeys) next = Math.min(Math.max(current + stepKeys[event.key], 0), ticks.length - 1);
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = ticks.length - 1;
    else return;
    event.preventDefault();
    ticks[next]?.focus();
  };

  return (
    <motion.div
      layoutRoot
      className={cn(
        '[:where(&)]:isolate [:where(&)]:relative [:where(&)]:flex [:where(&)]:w-full [:where(&)]:overflow-visible',
        isHorizontal
          ? '[:where(&)]:min-h-64 [:where(&)]:flex-col [:where(&)]:items-center [:where(&)]:justify-center'
          : '[:where(&)]:min-h-80',
        className,
      )}
    >
      <nav
        ref={railRef}
        aria-label={label}
        // Focus leaving the ticks (also into `children`) ends the focus
        // preview and any pinned one.
        onBlur={(event) => {
          if (!event.currentTarget.contains(event.relatedTarget)) {
            setFocusedId(null);
            setPinnedId(null);
            setRovingId(null);
          }
        }}
        onKeyDown={moveFocus}
        onScroll={(event) => setRailScroll(isHorizontal ? event.currentTarget.scrollLeft : event.currentTarget.scrollTop)}
        onPointerLeave={(event) => {
          if (hover.leave(event)) setHoveredId(null);
        }}
        style={isHorizontal ? { gridTemplateColumns: rowTemplate } : { gridTemplateRows: rowTemplate }}
        // `safe center` centres the ticks like the reference's content-center
        // while they fit, and starts them at the top (scrollable) once they don't.
        className={cn(
          '[:where(&)]:relative [:where(&)]:z-10 [:where(&)]:grid [:where(&)]:shrink-0',
          isHorizontal
            ? '[:where(&)]:h-12 [:where(&)]:w-fit [:where(&)]:max-w-full [:where(&)]:self-center [:where(&)]:justify-center'
            : '[:where(&)]:w-12 [:where(&)]:[align-content:safe_center]',
          railClassName,
        )}
      >
        {items.map((item, index) => {
          const selected = item.id === selectedId;
          const highlighted = item.id === highlightedId;
          const scale = tickScale(index, displayedIndex, highlighted);
          const itemContent = (
            <span
              data-slot="preview-rail-tick"
              aria-hidden="true"
              // A CSS transition, not a spring: the rail has a tick per
              // message, and a motion value each made every new page of
              // history (and every hover) diff a hundred animations.
              style={{ transform: isHorizontal ? `scaleY(${scale})` : `scaleX(${scale})` }}
              // forced-colors: the UA repaints backgrounds as Canvas, which
              // would erase the ticks; CanvasText keeps them drawn.
              className={cn(
                'block bg-current forced-colors:bg-[color:CanvasText] motion-safe:transition-transform motion-safe:duration-300 motion-safe:ease-(--ease-emphasized)',
                isHorizontal ? 'h-12 w-0.5 origin-bottom' : 'h-0.5 w-12 origin-left',
                highlighted ? 'text-ink' : undefined,
              )}
            />
          );
          // Focus ring: the app's global :focus-visible outline (2px accent,
          // offset 2px) draws the reference's ring-2 + ring-offset-2.
          const sharedClassName = cn(
            'relative flex text-ink-3',
            isHorizontal ? 'h-12 w-6 items-end justify-center' : 'h-6 w-12 items-center',
          );
          const sharedStyle = isHorizontal ? { width: itemSize } : { height: itemSize };
          const handlePointerEnter = (event) => {
            if (hover.enter(event)) setHoveredId(item.id);
          };
          const handlePointerDown = (event) => {
            tap.start(event, pinnedId === item.id);
            setFocusedId(null);
          };
          const dropGesture = () => tap.drop();
          const handleFocus = (currentTarget) => {
            if (currentTarget.matches(':focus-visible')) setFocusedId(item.id);
          };
          const handleSelect = (event) => {
            const gesture = tap.take();
            const tapped = gesture !== null && gesture.pointerType !== 'mouse';
            if (tapped) {
              // A first tap on a link only previews it; the second follows it.
              if (item.href && !gesture.state) {
                event.preventDefault();
                setPinnedId(item.id);
                return;
              }
              setPinnedId(item.id);
            }
            selectItem(item.id);
            onItemSelect?.(item);
          };
          const itemProps = {
            'data-slot': 'preview-rail-item',
            'aria-label': item.ariaLabel ?? item.label,
            'aria-current': selected ? 'location' : undefined,
            tabIndex: item.id === tabStopId ? 0 : -1,
            onPointerEnter: handlePointerEnter,
            onPointerDown: handlePointerDown,
            onPointerCancel: dropGesture,
            onKeyDown: dropGesture,
            onFocus: (event) => {
              setRovingId(item.id);
              handleFocus(event.currentTarget);
            },
            onClick: handleSelect,
            style: sharedStyle,
            className: sharedClassName,
          };
          return item.href ? (
            <a key={item.id} href={item.href} target={item.target} rel={item.rel} {...itemProps}>
              {itemContent}
            </a>
          ) : (
            <button key={item.id} type="button" {...itemProps}>
              {itemContent}
            </button>
          );
        })}
      </nav>
      {showPreview ? (
        <div
          aria-hidden="true"
          // Rows line up with the ticks; a scrolled (vertical) rail moves them along.
          style={isHorizontal
            ? { gridTemplateColumns: rowTemplate }
            : { gridTemplateRows: rowTemplate, transform: railScroll ? `translateY(${-railScroll}px)` : undefined }}
          className={cn(
            '[:where(&)]:pointer-events-none [:where(&)]:absolute [:where(&)]:z-50 [:where(&)]:grid',
            isHorizontal
              ? '[:where(&)]:top-1/2 [:where(&)]:left-1/2 [:where(&)]:h-5 [:where(&)]:w-fit [:where(&)]:max-w-full [:where(&)]:-translate-x-1/2 [:where(&)]:-translate-y-1/2 [:where(&)]:justify-center'
              : previewSide === 'before'
                ? '[:where(&)]:inset-y-0 [:where(&)]:right-16 [:where(&)]:left-4 [:where(&)]:[align-content:safe_center]'
                : '[:where(&)]:inset-y-0 [:where(&)]:right-4 [:where(&)]:left-16 [:where(&)]:[align-content:safe_center]',
            previewContainerClassName,
          )}
        >
          {items.map((item) => (
            <div
              key={item.id}
              style={isHorizontal ? { width: itemSize } : { height: itemSize }}
              className={cn('relative flex items-center', isHorizontal ? 'justify-center' : undefined)}
            >
              {item.id === displayedId ? (
                <div
                  className={cn(
                    isHorizontal
                      ? '[:where(&)]:absolute [:where(&)]:bottom-12 [:where(&)]:left-1/2 [:where(&)]:w-72 [:where(&)]:-translate-x-1/2'
                      : cn('[:where(&)]:w-full [:where(&)]:max-w-sm', previewSide === 'before' && '[:where(&)]:ml-auto'),
                    previewClassName,
                  )}
                >
                  <motion.div layoutId={`preview-rail-card-${uid}`} transition={reduce ? { duration: 0 } : SPRING_LAYOUT}>
                    <AnimatePresence mode="wait" initial={false}>
                      <motion.div
                        key={item.id}
                        initial={reduce ? { opacity: 0 } : { opacity: 0, y: 4, filter: 'blur(6px)' }}
                        animate={reduce ? { opacity: 1 } : { opacity: 1, y: 0, filter: 'blur(0px)' }}
                        exit={reduce
                          ? { opacity: 0 }
                          : { opacity: 0, y: -2, filter: 'blur(4px)', transition: { duration: 0.12, ease: EASE_OUT } }}
                        transition={{ duration: reduce ? 0 : 0.18, ease: EASE_OUT }}
                      >
                        {renderPreview ? renderPreview(item) : <DefaultPreview item={item} />}
                      </motion.div>
                    </AnimatePresence>
                  </motion.div>
                </div>
              ) : null}
            </div>
          ))}
        </div>
      ) : null}
      {children ? <div className="min-h-0 min-w-0 flex-1">{children}</div> : null}
    </motion.div>
  );
}

/* ------------------------------------------------------------------ */
/* MessageScroller                                                     */
/* ------------------------------------------------------------------ */

/** Whether keys pressed on `target` edit text instead of scrolling. */
function isEditable(target) {
  return target instanceof Element && Boolean(target.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"])'));
}

/** Text the rail previews: the bubble or content slot when present, else the whole row. */
function getMessageText(message) {
  const surface = message.querySelector('[data-slot="message-bubble-content"]')
    ?? message.querySelector('[data-slot="message-content"]')
    ?? message;
  return normalizeMessageText(surface.textContent);
}

/**
 * @typedef {object} MessageScrollerProps
 * @property {boolean} [followOutput] Keep new output in view while at the live edge (default true).
 * @property {number} [followThreshold] Live-edge distance in px (default 56).
 * @property {boolean} [smooth] Smooth-scroll when following and jumping (default true; never under reduced motion,
 *   and following is instant while `busy`).
 * @property {(following: boolean) => void} [onFollowChange] Called when the reader leaves or returns to the live edge.
 * @property {string} [label] Accessible name of the transcript section (default "Conversation").
 * @property {boolean} [busy] Sets aria-busy on the log while a reply is streaming (following is then instant).
 * @property {'rail'} [navigation] Adds the message rail.
 * @property {string} [navigationLabel] Rail landmark name (default "Message navigation").
 * @property {string} [viewportClassName] Classes for the scrolling <section>.
 * @property {string} [contentClassName] Classes for the role="log" content element.
 * @property {string} [railClassName] Extra classes for the rail <nav>.
 * @property {React.Ref<HTMLElement>} [viewportRef] Receives the scrolling <section>.
 * @property {React.HTMLAttributes<HTMLElement>} [viewportProps] Spread on the <section>
 *   (its onScroll, onWheel, onTouchStart and onKeyDown are chained; className is ignored, use viewportClassName).
 * @property {React.HTMLAttributes<HTMLDivElement>} [contentProps] Spread last on the log element.
 * @property {string} [className] Root classes.
 * @property {React.ReactNode} [children] Messages: rows with data-slot="message" and data-from.
 */

/**
 * The transcript viewport. Rows marked `data-slot="message"` (with
 * `data-from="user" | "assistant"`) feed the rail; wrap the message text in
 * `data-slot="message-content"` (or `message-bubble-content`) to keep author
 * labels and actions out of previews. Give it a bounded height (`min-h-0` in
 * a flex column) and keep the composer outside it.
 *
 * Other props go to the root <div data-slot="message-scroller">, and `ref`
 * to that root.
 */
export const MessageScroller = forwardRef(
  /**
   * @param {MessageScrollerProps & Omit<React.HTMLAttributes<HTMLDivElement>, 'children'>} props
   * @param {React.Ref<HTMLDivElement>} ref
   */
  function MessageScroller({
    followOutput = true,
    followThreshold = DEFAULT_FOLLOW_THRESHOLD,
    smooth = true,
    onFollowChange,
    label = 'Conversation',
    busy,
    navigation,
    navigationLabel = 'Message navigation',
    viewportClassName,
    contentClassName,
    railClassName,
    viewportRef: externalViewportRef,
    viewportProps,
    contentProps,
    className,
    children,
    ...props
  }, ref) {
    const reduce = useReducedMotion() ?? false;
    const viewportRef = useRef(null);
    const contentRef = useRef(null);
    const followingRef = useRef(followOutput);
    const programmaticScrollRef = useRef(false);
    const scrollTimerRef = useRef(undefined);
    const releaseFrameRef = useRef(undefined);
    const frameRef = useRef(undefined);
    const railFrameRef = useRef(undefined);
    const railIdRef = useRef(new WeakMap());
    const railIdCounterRef = useRef(0);
    const railTargetsRef = useRef(new Map());
    const [railItems, setRailItems] = useState([]);
    const [activeRailId, setActiveRailId] = useState('');
    // Assumed until the first measurement: a conversation that opens with
    // history nearly always overflows, and reserving the rail's gutter only
    // after that measurement narrowed the text a frame after the first paint
    // (on a phone every message re-wrapped and the transcript grew a sixth).
    const [railOverflowing, setRailOverflowing] = useState(navigation === 'rail');
    // The transcript's height: the rail fits its ticks into it.
    const [railSpace, setRailSpace] = useState(0);
    const {
      onScroll: onViewportScroll,
      onWheel: onViewportWheel,
      onTouchStart: onViewportTouchStart,
      onKeyDown: onViewportKeyDown,
      ...restViewportProps
    } = viewportProps ?? {};

    // React 18: a plain callback ref (React calls it with null on detach)
    // that also forwards the node to the caller's viewportRef.
    const setViewportRef = useCallback((node) => {
      viewportRef.current = node;
      if (typeof externalViewportRef === 'function') externalViewportRef(node);
      else if (externalViewportRef) externalViewportRef.current = node;
    }, [externalViewportRef]);

    const setFollowing = useCallback((next) => {
      if (followingRef.current === next) return;
      followingRef.current = next;
      onFollowChange?.(next);
    }, [onFollowChange]);

    const updateActiveRailItem = useCallback(() => {
      if (navigation !== 'rail') return;
      const viewport = viewportRef.current;
      const targets = railTargetsRef.current;
      if (!viewport || targets.size === 0) return;
      const viewportRect = viewport.getBoundingClientRect();
      const nextId = pickActiveRailId({
        scrollHeight: viewport.scrollHeight,
        scrollTop: viewport.scrollTop,
        clientHeight: viewport.clientHeight,
        threshold: followThreshold,
        viewportCenter: viewportRect.top + viewportRect.height / 2,
        ids: [...targets.keys()],
        measure: (id) => targets.get(id).getBoundingClientRect(),
      });
      if (nextId !== null) setActiveRailId((current) => (current === nextId ? current : nextId));
    }, [followThreshold, navigation]);

    const syncRailItems = useCallback(() => {
      if (navigation !== 'rail') return;
      const content = contentRef.current;
      const viewport = viewportRef.current;
      if (!content || !viewport) return;
      const messages = Array.from(content.querySelectorAll('[data-slot="message"]'));
      const targets = new Map();
      const described = describeRailMessages(messages.map((message) => ({
        from: message.dataset.from,
        text: getMessageText(message),
      })));
      const nextItems = messages.map((message, index) => {
        let id = railIdRef.current.get(message);
        if (!id) {
          railIdCounterRef.current += 1;
          id = `message-rail-${railIdCounterRef.current}`;
          railIdRef.current.set(message, id);
        }
        targets.set(id, message);
        return { id, ...described[index] };
      });
      railTargetsRef.current = targets;
      setRailItems((current) => (railItemsEqual(current, nextItems) ? current : nextItems));
      setRailOverflowing(isRailOverflowing(viewport, messages.length));
      setRailSpace(viewport.clientHeight);
    }, [navigation]);

    const scheduleRailSync = useCallback(() => {
      if (navigation !== 'rail') return;
      if (railFrameRef.current) cancelAnimationFrame(railFrameRef.current);
      railFrameRef.current = requestAnimationFrame(() => {
        syncRailItems();
        updateActiveRailItem();
      });
    }, [navigation, syncRailItems, updateActiveRailItem]);

    // The scroll handler ignores this component's own scroll until it comes
    // to rest: for an instant one, until the next frame has dispatched its
    // scroll event (the reference's next task comes first, and that event
    // would then read output added since as the reader leaving the live
    // edge: frequent now that following is instant while busy); for a
    // smooth one the reference's SMOOTH_SCROLL_SETTLE_MS, then for as long
    // as it is still moving (a long jump outlasts that window, and its late
    // events would otherwise stop following just before new output arrives).
    const cancelRelease = useCallback(() => {
      if (scrollTimerRef.current) window.clearTimeout(scrollTimerRef.current);
      if (releaseFrameRef.current) cancelAnimationFrame(releaseFrameRef.current);
      scrollTimerRef.current = undefined;
      releaseFrameRef.current = undefined;
    }, []);

    // `markLanding`: once released, mark the message the scroll landed on in
    // the rail (follow scrolls; a rail jump has already marked its target).
    const releaseProgrammaticScroll = useCallback((behavior, markLanding = false) => {
      cancelRelease();
      if (behavior !== 'smooth') {
        // Scroll events go out before a frame's animation callbacks.
        releaseFrameRef.current = requestAnimationFrame(() => {
          releaseFrameRef.current = undefined;
          programmaticScrollRef.current = false;
          if (markLanding) updateActiveRailItem();
        });
        return;
      }
      const startedAt = Date.now();
      let previousScrollTop = null;
      const check = () => {
        const scrollTop = viewportRef.current?.scrollTop;
        if (scrollTop === undefined || smoothScrollSettled({ scrollTop, previousScrollTop, elapsedMs: Date.now() - startedAt })) {
          programmaticScrollRef.current = false;
          // The scroll events it sent were ignored: mark where it landed.
          if (markLanding) updateActiveRailItem();
          return;
        }
        previousScrollTop = scrollTop;
        scrollTimerRef.current = window.setTimeout(check, SMOOTH_SCROLL_POLL_MS);
      };
      scrollTimerRef.current = window.setTimeout(check, SMOOTH_SCROLL_SETTLE_MS);
    }, [cancelRelease, updateActiveRailItem]);

    const scrollToEnd = useCallback((behavior) => {
      const viewport = viewportRef.current;
      if (!viewport) return;
      programmaticScrollRef.current = true;
      if (typeof viewport.scrollTo === 'function') viewport.scrollTo({ top: viewport.scrollHeight, behavior });
      else viewport.scrollTop = viewport.scrollHeight;
      releaseProgrammaticScroll(behavior, true);
    }, [releaseProgrammaticScroll]);

    const handleScroll = useCallback(() => {
      const viewport = viewportRef.current;
      if (!viewport || programmaticScrollRef.current) return;
      setFollowing(distanceFromEnd(viewport) <= followThreshold);
      updateActiveRailItem();
    }, [followThreshold, setFollowing, updateActiveRailItem]);

    // Wheel, touch and ArrowUp/PageUp/Home are the reader's own scrolls: stop
    // ignoring scroll events so the next one can leave the live edge. A
    // follow (or rail) scroll still gliding is stopped where it is, or it
    // would carry the view back to the end over the reader's scroll. An
    // upward wheel or a leave key pauses following at once, so output that
    // arrives before the reader's scroll lands cannot pull the view down;
    // where that scroll lands then decides (within the threshold resumes).
    const leaveLiveEdge = useCallback((pause) => {
      cancelRelease();
      const gliding = programmaticScrollRef.current;
      programmaticScrollRef.current = false;
      const viewport = viewportRef.current;
      if (!viewport) return;
      if (gliding && typeof viewport.scrollTo === 'function') viewport.scrollTo({ top: viewport.scrollTop, behavior: 'instant' });
      if (pause && viewport.scrollTop > 0) setFollowing(false);
    }, [cancelRelease, setFollowing]);

    useLayoutEffect(() => {
      followingRef.current = followOutput;
      if (!followOutput) return undefined;
      frameRef.current = requestAnimationFrame(() => scrollToEnd('auto'));
      return () => {
        if (frameRef.current) cancelAnimationFrame(frameRef.current);
      };
    }, [followOutput, scrollToEnd]);

    // While a reply streams in (`busy`), following keeps up instantly: a
    // smooth scroll still gliding to the end makes the browser drop the
    // reader's wheel (Chrome), so scrolling up could not pause it. Smooth
    // again once the reply is done, and for rail jumps (a deviation from
    // the reference, which glides here too).
    const busyRef = useRef(busy);
    busyRef.current = busy;

    const openedAtRef = useRef(0);
    useEffect(() => {
      openedAtRef.current = performance.now();
    }, []);

    useEffect(() => {
      const content = contentRef.current;
      if (!content || typeof ResizeObserver === 'undefined') return undefined;
      const observer = new ResizeObserver(() => {
        scheduleRailSync();
        if (!followOutput || !followingRef.current) return;
        const settling = performance.now() - openedAtRef.current < OPENING_SETTLE_MS;
        scrollToEnd(reduce || !smooth || busyRef.current || settling ? 'auto' : 'smooth');
      });
      observer.observe(content);
      return () => observer.disconnect();
    }, [followOutput, reduce, scheduleRailSync, scrollToEnd, smooth]);

    useEffect(() => {
      if (navigation !== 'rail') {
        railTargetsRef.current.clear();
        return undefined;
      }
      const content = contentRef.current;
      const viewport = viewportRef.current;
      if (!content || !viewport) return undefined;
      scheduleRailSync();
      const mutationObserver = typeof MutationObserver === 'undefined' ? null : new MutationObserver(scheduleRailSync);
      mutationObserver?.observe(content, { childList: true, characterData: true, subtree: true });
      const resizeObserver = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(scheduleRailSync);
      resizeObserver?.observe(content);
      resizeObserver?.observe(viewport);
      return () => {
        mutationObserver?.disconnect();
        resizeObserver?.disconnect();
      };
    }, [navigation, scheduleRailSync]);

    useEffect(() => () => {
      if (scrollTimerRef.current) window.clearTimeout(scrollTimerRef.current);
      if (releaseFrameRef.current) cancelAnimationFrame(releaseFrameRef.current);
      if (frameRef.current) cancelAnimationFrame(frameRef.current);
      if (railFrameRef.current) cancelAnimationFrame(railFrameRef.current);
    }, []);

    const scrollToRailItem = useCallback((item) => {
      const viewport = viewportRef.current;
      const target = railTargetsRef.current.get(item.id);
      if (!viewport || !target) return;
      const lastItem = railItems.at(-1)?.id === item.id;
      setActiveRailId(item.id);
      if (lastItem) {
        // The last tick returns to the live edge and resumes following.
        setFollowing(true);
        scrollToEnd(reduce || !smooth ? 'auto' : 'smooth');
        return;
      }
      setFollowing(false);
      programmaticScrollRef.current = true;
      const viewportRect = viewport.getBoundingClientRect();
      const targetRect = target.getBoundingClientRect();
      const top = centeredScrollTop({
        scrollTop: viewport.scrollTop,
        clientHeight: viewport.clientHeight,
        viewportTop: viewportRect.top,
        targetTop: targetRect.top,
        targetHeight: targetRect.height,
      });
      const behavior = reduce || !smooth ? 'auto' : 'smooth';
      if (typeof viewport.scrollTo === 'function') viewport.scrollTo({ top, behavior });
      else viewport.scrollTop = top;
      releaseProgrammaticScroll(behavior);
    }, [railItems, reduce, releaseProgrammaticScroll, scrollToEnd, setFollowing, smooth]);

    const rail = navigation === 'rail';
    const shownRailItems = railOverflowing ? railItems : [];
    // Ticks shrink to fit the transcript's height (14px rows while they fit,
    // never below 4px); a rail that still does not fit scrolls, and its
    // focus ring is drawn inside the tick so the scrolling rail cannot clip it.
    const railItemSize = fitRailItemSize(railSpace, shownRailItems.length);
    const railScrolls = shownRailItems.length * railItemSize > railSpace - RAIL_INSET;
    const viewport = (
      <section
        ref={setViewportRef}
        aria-label={label}
        tabIndex={0}
        {...restViewportProps}
        onScroll={(event) => {
          handleScroll();
          onViewportScroll?.(event);
        }}
        onWheel={(event) => {
          leaveLiveEdge(event.deltaY < 0);
          onViewportWheel?.(event);
        }}
        onTouchStart={(event) => {
          leaveLiveEdge(false);
          onViewportTouchStart?.(event);
        }}
        onKeyDown={(event) => {
          // A key a control inside already handled, or one typed into a
          // field, does not scroll the transcript.
          if (isLeaveLiveEdgeKey(event.key)) leaveLiveEdge(!event.defaultPrevented && !isEditable(event.target));
          onViewportKeyDown?.(event);
        }}
        className={cn(
          // The app's global :focus-visible outline, drawn inside the edge
          // (the reference's inset ring) so the clipping root cannot hide it.
          '[:where(&)]:h-full [:where(&)]:overflow-y-auto [:where(&)]:overscroll-contain [:where(&)]:[overflow-anchor:none] focus-visible:outline-offset-[-2px]',
          rail ? '[-ms-overflow-style:none] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden' : '[scrollbar-gutter:stable]',
          viewportClassName,
          // Doubled specificity: the rail's gutter must beat any padding in viewportClassName.
          rail && railOverflowing && '[&&]:pr-10',
        )}
      >
        <div
          ref={contentRef}
          role="log"
          aria-live="polite"
          aria-relevant="additions text"
          aria-busy={busy}
          className={contentClassName}
          {...contentProps}
        >
          {children}
        </div>
      </section>
    );

    return (
      <div ref={ref} data-slot="message-scroller" className={cn('[:where(&)]:min-h-0', className)} {...props}>
        {rail ? (
          <PreviewRail
            items={shownRailItems}
            label={navigationLabel}
            activeId={activeRailId}
            onItemSelect={scrollToRailItem}
            previewSide="before"
            highlightActive
            itemSize={railItemSize}
            className="h-full min-h-0 overflow-hidden"
            // inset-y-4: the rows start where the ticks do (inset-y-3 + py-1), also once the rail scrolls.
            previewContainerClassName="inset-y-4 right-8 left-3"
            previewClassName="mr-1 w-64 max-w-full [&_[data-slot=preview-rail-card]]:h-20 [&_[data-slot=preview-rail-card]]:overflow-hidden [&_[data-slot=preview-rail-card]]:p-3 [&_[data-slot=preview-rail-title]]:line-clamp-1 [&_[data-slot=preview-rail-title]]:text-xs [&_[data-slot=preview-rail-title]]:leading-4 [&_[data-slot=preview-rail-description]]:line-clamp-2 [&_[data-slot=preview-rail-description]]:text-xs [&_[data-slot=preview-rail-description]]:leading-4"
            railClassName={cn(
              'absolute inset-y-3 right-1 w-7 py-1 [&_[data-slot=preview-rail-item]]:w-7 [&_[data-slot=preview-rail-item]]:justify-end [&_[data-slot=preview-rail-tick]]:h-px [&_[data-slot=preview-rail-tick]]:w-4 [&_[data-slot=preview-rail-tick]]:origin-right',
              // Hidden (not just transparent) without overflow, so no empty landmark stays in the tree.
              railOverflowing ? 'pointer-events-auto opacity-100' : 'pointer-events-none invisible opacity-0',
              railScrolls && 'overflow-y-auto overscroll-contain [-ms-overflow-style:none] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden [&_[data-slot=preview-rail-item]]:focus-visible:outline-offset-[-2px]',
              railClassName,
            )}
          >
            {viewport}
          </PreviewRail>
        ) : viewport}
      </div>
    );
  },
);

MessageScroller.displayName = 'MessageScroller';

export default MessageScroller;
