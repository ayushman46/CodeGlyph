import { AnimatePresence, motion, useIsPresent, useReducedMotion } from 'framer-motion';
import {
  cloneElement, createContext, forwardRef, isValidElement, useCallback, useContext, useEffect, useId,
  useLayoutEffect, useMemo, useRef, useState,
} from 'react';
import { createPortal } from 'react-dom';
import { cn } from '../../lib/cn';

/*
 * Popover: a non-modal dialog anchored to a trigger button (the Base UI
 * Popover semantics the Honest UI references rely on), plus the floating
 * helpers Menu.jsx shares: anchored positioning with flip, the dismiss layer
 * stack (Escape, outside press, focus leaving), Tab order that continues
 * after the trigger, and `render` prop merging.
 */

/** Distance kept between a floating layer and the viewport edge (2rem total). */
export const VIEWPORT_PADDING = 16;
/** --hui-ease-out / --hui-duration-normal of the reference popups. */
export const POPUP_EASE = [0.22, 1, 0.36, 1];
const POPUP_SECONDS = 0.2;
/** Above the Modal (z-[80]) so popovers inside dialogs still show. */
const POPUP_Z_INDEX = 90;

const TABBABLE = [
  'a[href]', 'area[href]', 'button:not([disabled])', 'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])', 'textarea:not([disabled])', 'iframe', 'audio[controls]', 'video[controls]',
  '[contenteditable]:not([contenteditable="false"])', '[tabindex]',
].join(',');

/** Elements in `container` that Tab can reach, in document order. */
export const getTabbables = (container) => {
  if (!container) return [];
  return [...container.querySelectorAll(TABBABLE)].filter((node) => (
    node.tabIndex >= 0 && !node.closest('[hidden],[inert]') && node.getClientRects().length > 0
  ));
};

/**
 * Moves focus to the element Tab would reach after `anchor` (or before it),
 * skipping anything inside `skip`. Floating layers live in a portal at the
 * end of <body>; this keeps their Tab order as if they sat right after the
 * element that opened them.
 */
export const focusBeside = (anchor, { backwards = false, skip } = {}) => {
  if (!anchor?.isConnected) return;
  const all = getTabbables(document.body).filter((node) => node === anchor || !skip?.(node));
  const index = all.indexOf(anchor);
  const target = backwards ? all[index - 1] : all[index + 1];
  (index < 0 || !target ? anchor : target).focus();
};

/** One callback ref that feeds several refs. */
export const mergeRefs = (...refs) => (node) => {
  refs.forEach((ref) => {
    if (typeof ref === 'function') ref(node);
    else if (ref) ref.current = node;
  });
};

const chain = (...handlers) => (event) => handlers.forEach((handler) => handler?.(event));

/**
 * Renders `render` (a React element such as `<Button variant="link" />`) with
 * `props` merged in, Base UI style: classes are joined, event handlers both
 * run (the element's first), styles merge and refs are combined.
 */
export const renderElement = (render, props, ref) => {
  if (!isValidElement(render)) return null;
  const own = render.props || {};
  const merged = { ...own, ...props };
  merged.className = cn(own.className, props.className) || undefined;
  if (own.style || props.style) merged.style = { ...own.style, ...props.style };
  Object.keys(props).forEach((key) => {
    if (/^on[A-Z]/.test(key) && typeof own[key] === 'function' && typeof props[key] === 'function') {
      merged[key] = chain(own[key], props[key]);
    }
  });
  if (props.children === undefined) merged.children = own.children;
  return cloneElement(render, { ...merged, ref: mergeRefs(render.ref, ref) });
};

/** Whether a pointer event comes from a hovering pointer: not touch, no button down. */
export const isHoveringPointer = (event) => event.pointerType !== 'touch' && event.buttons === 0;

/* ------------------------------------------------------------------ */
/* Anchored positioning                                                */
/* ------------------------------------------------------------------ */

const samePosition = (a, b) => a && b && a.top === b.top && a.left === b.left && a.side === b.side
  && a.availableHeight === b.availableHeight && a.origin === b.origin && a.anchorWidth === b.anchorWidth;

/**
 * Fixed-position coordinates for `floatingRef` next to `anchorRef`: below
 * (or above) the anchor, aligned start | center | end, flipped to the side
 * with room, kept 16px inside the viewport, and refreshed on scroll, resize
 * and size changes. Returns null until the first measurement.
 */
export function useAnchoredPosition(anchorRef, floatingRef, {
  enabled = true, side = 'bottom', align = 'center', sideOffset = 4, alignOffset = 0,
} = {}) {
  const [position, setPosition] = useState(null);

  const update = useCallback(() => {
    const anchor = anchorRef.current;
    const floating = floatingRef.current;
    if (!anchor || !floating) return;
    const rect = anchor.getBoundingClientRect();
    const viewportWidth = document.documentElement.clientWidth;
    const viewportHeight = window.innerHeight;
    const width = floating.offsetWidth;
    // Natural height, even while a max-height is clipping it.
    const borders = floating.offsetHeight - floating.clientHeight;
    const height = floating.scrollHeight + borders;
    const below = viewportHeight - rect.bottom - sideOffset - VIEWPORT_PADDING;
    const above = rect.top - sideOffset - VIEWPORT_PADDING;
    let finalSide = side === 'top' ? 'top' : 'bottom';
    if (finalSide === 'bottom' && height > below && above > below) finalSide = 'top';
    else if (finalSide === 'top' && height > above && below > above) finalSide = 'bottom';
    const availableHeight = Math.max(0, Math.floor(finalSide === 'bottom' ? below : above));
    const shownHeight = Math.min(height, availableHeight);
    const top = finalSide === 'bottom' ? rect.bottom + sideOffset : rect.top - sideOffset - shownHeight;
    let left;
    if (align === 'start') left = rect.left + alignOffset;
    else if (align === 'end') left = rect.right - width - alignOffset;
    else left = rect.left + rect.width / 2 - width / 2;
    const maxLeft = viewportWidth - VIEWPORT_PADDING - width;
    left = maxLeft < VIEWPORT_PADDING ? VIEWPORT_PADDING : Math.min(Math.max(left, VIEWPORT_PADDING), maxLeft);
    // Grow from the point nearest the anchor.
    const originX = Math.min(Math.max(rect.left + rect.width / 2 - left, 0), width);
    const next = {
      top: Math.round(top),
      left: Math.round(left),
      side: finalSide,
      availableHeight,
      anchorWidth: Math.round(rect.width),
      origin: `${Math.round(originX)}px ${finalSide === 'bottom' ? '0px' : '100%'}`,
    };
    setPosition((current) => (samePosition(current, next) ? current : next));
  }, [anchorRef, floatingRef, side, align, sideOffset, alignOffset]);

  useLayoutEffect(() => {
    if (!enabled) return undefined;
    update();
    let frame = 0;
    const schedule = () => {
      if (frame) return;
      frame = window.requestAnimationFrame(() => { frame = 0; update(); });
    };
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(schedule);
    if (anchorRef.current) observer?.observe(anchorRef.current);
    if (floatingRef.current) observer?.observe(floatingRef.current);
    window.addEventListener('resize', schedule);
    window.addEventListener('scroll', schedule, true);
    return () => {
      if (frame) window.cancelAnimationFrame(frame);
      observer?.disconnect();
      window.removeEventListener('resize', schedule);
      window.removeEventListener('scroll', schedule, true);
    };
  }, [enabled, update, anchorRef, floatingRef]);

  return position;
}

/* ------------------------------------------------------------------ */
/* Dismiss layers                                                      */
/* ------------------------------------------------------------------ */

/** Open layers, innermost last: only the top one answers Escape. */
const layerStack = [];
const consumedPresses = new WeakSet();

/**
 * Swallows the click that follows `source` (an outside pointerdown), so a
 * press that dismisses a modal layer does not also activate what is under
 * it. Released by the next press, a cancelled pointer or any key.
 */
const consumeActivation = (source) => {
  const release = () => {
    window.removeEventListener('click', swallow, true);
    window.removeEventListener('pointerdown', restart, true);
    window.removeEventListener('pointercancel', restart, true);
    window.removeEventListener('keydown', release, true);
  };
  const swallow = (event) => {
    event.preventDefault();
    event.stopPropagation();
    release();
  };
  const restart = (event) => { if (event !== source) release(); };
  window.addEventListener('click', swallow, true);
  window.addEventListener('pointerdown', restart, true);
  window.addEventListener('pointercancel', restart, true);
  window.addEventListener('keydown', release, true);
};

/**
 * A parent layer's registry for floating layers opened from inside it (a
 * menu inside a popover): presses and focus inside a child count as inside
 * the parent, so opening the child never dismisses the parent.
 */
export const LayerContext = /* @__PURE__ */ createContext(null);

/**
 * Dismiss behaviour shared by Popover and Menu while `open`:
 * - Escape closes the top layer. It is consumed (so a dialog behind does not
 *   close too) when focus is in the layer or on its trigger, or the layer
 *   was not opened by hover.
 * - A press outside the trigger, the layer and its child layers closes it.
 *   With `consumeOutsidePress` (modal menus) that press does not also
 *   click what is under it.
 * - Focus moving from inside the layer to somewhere outside closes it.
 * - When it closes while focus is inside, focus returns to the trigger
 *   once the press that closed it has settled (unless focus already moved
 *   to something else).
 * Returns the LayerContext value to provide to the layer's content.
 */
export function useDismissableLayer({
  open, popupRef, triggerRef, getReason, onDismiss, consumeOutsidePress = false,
}) {
  const parentLayer = useContext(LayerContext);
  const childrenRef = useRef(new Set());
  const onDismissRef = useRef(onDismiss);
  onDismissRef.current = onDismiss;
  const getReasonRef = useRef(getReason);
  getReasonRef.current = getReason;

  const contains = useCallback((target) => {
    if (!target) return false;
    if (popupRef.current?.contains(target)) return true;
    for (const child of childrenRef.current) {
      if (child(target)) return true;
    }
    return false;
  }, [popupRef]);

  const layerApi = useMemo(() => ({
    register: (fn) => {
      childrenRef.current.add(fn);
      return () => childrenRef.current.delete(fn);
    },
  }), []);

  useEffect(() => {
    if (!open) return undefined;
    const token = { contains };
    layerStack.push(token);
    const unregister = parentLayer?.register(contains);
    const inTrigger = (target) => Boolean(target && triggerRef.current?.contains(target));
    let focusWasInside = contains(document.activeElement) || inTrigger(document.activeElement);

    const onKeyDown = (event) => {
      if (event.key !== 'Escape' || layerStack[layerStack.length - 1] !== token) return;
      const active = document.activeElement;
      if (contains(active) || inTrigger(active) || getReasonRef.current?.() !== 'hover') {
        event.preventDefault();
        event.stopPropagation();
      }
      onDismissRef.current('escape');
    };
    const onPointerDown = (event) => {
      const { target } = event;
      if (!(target instanceof Node) || inTrigger(target) || contains(target)) return;
      // One swallow per press, however many modal layers it dismisses.
      if (consumeOutsidePress && !consumedPresses.has(event)) {
        consumedPresses.add(event);
        consumeActivation(event);
      }
      onDismissRef.current('outside-press');
    };
    const onFocusIn = (event) => {
      const { target } = event;
      if (contains(target) || inTrigger(target)) {
        focusWasInside = true;
        return;
      }
      if (focusWasInside) onDismissRef.current('focus-out');
    };
    // Capture on window runs before the dialogs' document listeners.
    window.addEventListener('keydown', onKeyDown, true);
    window.addEventListener('pointerdown', onPointerDown, true);
    document.addEventListener('focusin', onFocusIn);
    return () => {
      const index = layerStack.indexOf(token);
      if (index >= 0) layerStack.splice(index, 1);
      unregister?.();
      window.removeEventListener('keydown', onKeyDown, true);
      window.removeEventListener('pointerdown', onPointerDown, true);
      document.removeEventListener('focusin', onFocusIn);
    };
  }, [open, contains, parentLayer, triggerRef, consumeOutsidePress]);

  // Return focus to the trigger when the layer closes with focus inside it.
  const wasOpen = useRef(open);
  useLayoutEffect(() => {
    const closed = wasOpen.current && !open;
    wasOpen.current = open;
    if (!closed) return undefined;
    if (!contains(document.activeElement)) return undefined;
    // After the closing press: a press on another focusable element keeps
    // the focus it moved there; a press on plain content would leave focus
    // on <body>, so it comes back to the trigger instead.
    const timer = window.setTimeout(() => {
      const active = document.activeElement;
      if (!active || active === document.body || !active.isConnected || contains(active)) {
        triggerRef.current?.focus({ preventScroll: true });
      }
    }, 0);
    return () => window.clearTimeout(timer);
  }, [open, contains, triggerRef]);

  return layerApi;
}

/* ------------------------------------------------------------------ */
/* Popover                                                             */
/* ------------------------------------------------------------------ */

const PopoverContext = /* @__PURE__ */ createContext(null);

const usePopoverContext = (name) => {
  const context = useContext(PopoverContext);
  if (!context) throw new Error(`<${name}> must be used inside <Popover>.`);
  return context;
};

const OPEN_CHANGE_REASON = {
  press: 'trigger-press',
  keyboard: 'trigger-press',
  hover: 'trigger-hover',
  'trigger-press': 'trigger-press',
  'trigger-hover': 'trigger-hover',
  escape: 'escape-key',
  'outside-press': 'outside-press',
  'focus-out': 'focus-out',
};

/**
 * Root: holds the open state (controlled with `open` + `onOpenChange`, or
 * uncontrolled from `defaultOpen`). Renders no element.
 *
 * @param {object} props
 * @param {boolean} [props.open]
 * @param {boolean} [props.defaultOpen=false]
 * @param {(open: boolean, details: { reason: string }) => void} [props.onOpenChange]
 *   reason: 'trigger-press' | 'trigger-hover' | 'escape-key' | 'outside-press' | 'focus-out'
 * @param {import('react').ReactNode} props.children
 */
export function Popover({ open: openProp, defaultOpen = false, onOpenChange, children }) {
  const isControlled = openProp !== undefined;
  const [uncontrolledOpen, setUncontrolledOpen] = useState(defaultOpen);
  const open = isControlled ? Boolean(openProp) : uncontrolledOpen;
  // How the current opening happened: 'press' | 'keyboard' | 'hover' | null (opened from outside).
  const reasonRef = useRef(null);
  const triggerRef = useRef(null);
  const popupRef = useRef(null);
  const triggerIdRef = useRef(undefined);
  const popupId = useId();
  const hoverRef = useRef({ enabled: false, delay: 300, closeDelay: 100 });
  const timers = useRef({ open: 0, close: 0 });
  const openRef = useRef(open);
  openRef.current = open;
  const onOpenChangeRef = useRef(onOpenChange);
  onOpenChangeRef.current = onOpenChange;
  const controlledRef = useRef(isControlled);
  controlledRef.current = isControlled;

  const clearTimers = useCallback(() => {
    window.clearTimeout(timers.current.open);
    window.clearTimeout(timers.current.close);
    timers.current = { open: 0, close: 0 };
  }, []);
  useEffect(() => clearTimers, [clearTimers]);

  const setOpen = useCallback((next, why) => {
    clearTimers();
    if (next === openRef.current) {
      // A press on a popover opened by hover keeps it open ("sticks").
      if (next) reasonRef.current = why;
      return;
    }
    reasonRef.current = next ? why : null;
    openRef.current = next;
    if (!controlledRef.current) setUncontrolledOpen(next);
    onOpenChangeRef.current?.(next, { reason: OPEN_CHANGE_REASON[why] || why });
  }, [clearTimers]);

  const scheduleOpen = useCallback(() => {
    window.clearTimeout(timers.current.close);
    if (openRef.current || timers.current.open) return;
    timers.current.open = window.setTimeout(() => {
      timers.current.open = 0;
      setOpen(true, 'hover');
    }, hoverRef.current.delay);
  }, [setOpen]);

  const scheduleClose = useCallback(() => {
    window.clearTimeout(timers.current.open);
    timers.current.open = 0;
    if (!openRef.current || reasonRef.current !== 'hover') return;
    window.clearTimeout(timers.current.close);
    timers.current.close = window.setTimeout(() => setOpen(false, 'hover'), hoverRef.current.closeDelay);
  }, [setOpen]);

  const cancelClose = useCallback(() => {
    window.clearTimeout(timers.current.close);
    timers.current.close = 0;
  }, []);

  const layerApi = useDismissableLayer({
    open,
    popupRef,
    triggerRef,
    getReason: () => reasonRef.current,
    onDismiss: (why) => setOpen(false, why),
  });

  const value = useMemo(() => ({
    open, setOpen, reasonRef, triggerRef, popupRef, triggerIdRef, popupId, hoverRef,
    scheduleOpen, scheduleClose, cancelClose, layerApi,
  }), [open, setOpen, popupId, scheduleOpen, scheduleClose, cancelClose, layerApi]);

  return <PopoverContext.Provider value={value}>{children}</PopoverContext.Provider>;
}

/**
 * The button that opens the popup. Gets aria-haspopup="dialog",
 * aria-expanded, aria-controls (while open) and `data-popup-open` (while
 * open). Press or Enter/Space toggles it and moves focus into the popup;
 * with `openOnHover` a hovering pointer also opens it after `delay` ms and
 * closes it `closeDelay` ms after leaving both trigger and popup (a press
 * while hover-opened keeps it open).
 *
 * @param {object} props
 * @param {boolean} [props.openOnHover=false]
 * @param {number} [props.delay=300] hover open delay in ms
 * @param {number} [props.closeDelay=100] hover close grace in ms
 * @param {import('react').ReactElement} [props.render] element to render instead of <button>, e.g. <Button variant="link" />
 */
export const PopoverTrigger = /* @__PURE__ */ forwardRef(function PopoverTrigger({
  openOnHover = false, delay = 300, closeDelay = 100, render, className, children, disabled,
  type, onClick, onPointerEnter, onPointerLeave, id, ...props
}, ref) {
  const context = usePopoverContext('PopoverTrigger');
  const generatedId = useId();
  const triggerId = id || generatedId;
  context.triggerIdRef.current = triggerId;
  context.hoverRef.current = { enabled: openOnHover, delay, closeDelay };
  const setRef = useMemo(() => mergeRefs(ref, context.triggerRef), [ref, context.triggerRef]);

  const handleClick = (event) => {
    if (disabled || event.defaultPrevented) return;
    const viaKeyboard = event.detail === 0;
    if (context.open) {
      if (context.reasonRef.current === 'hover' && !viaKeyboard) {
        context.setOpen(true, 'press');
        return;
      }
      context.setOpen(false, 'trigger-press');
    } else {
      context.setOpen(true, viaKeyboard ? 'keyboard' : 'press');
    }
  };
  const handlePointerEnter = (event) => {
    if (!openOnHover || disabled || !isHoveringPointer(event)) return;
    context.scheduleOpen();
  };
  const handlePointerLeave = (event) => {
    if (!openOnHover || event.pointerType === 'touch') return;
    context.scheduleClose();
  };

  const triggerProps = {
    ...props,
    id: triggerId,
    className,
    disabled,
    'aria-haspopup': 'dialog',
    'aria-expanded': context.open,
    'aria-controls': context.open ? context.popupId : undefined,
    'data-popup-open': context.open ? '' : undefined,
    'data-slot': props['data-slot'] ?? 'popover-trigger',
    onClick: chain(onClick, handleClick),
    onPointerEnter: chain(onPointerEnter, handlePointerEnter),
    onPointerLeave: chain(onPointerLeave, handlePointerLeave),
  };
  if (render) return renderElement(render, { ...triggerProps, children }, setRef);
  return (
    <button ref={setRef} type={type ?? 'button'} {...triggerProps}>
      {children}
    </button>
  );
});

function PopoverPopupInner({
  context, align, side, sideOffset, alignOffset, initialFocus, className, style, children,
  onKeyDown, onPointerEnter, onPointerLeave, 'aria-label': ariaLabel, 'aria-labelledby': ariaLabelledBy, ...props
}) {
  const reduceMotion = useReducedMotion() ?? false;
  const isPresent = useIsPresent();
  const { popupRef, triggerRef } = context;
  const position = useAnchoredPosition(triggerRef, popupRef, { side, align, sideOffset, alignOffset });

  // Opened by a press or the keyboard: focus moves in. Opened by hover: it
  // stays put. Waits for the first position: a still-hidden popup cannot
  // take focus.
  const placed = position !== null;
  const focusedRef = useRef(false);
  useEffect(() => {
    if (!placed || focusedRef.current) return;
    focusedRef.current = true;
    const reason = context.reasonRef.current;
    if (reason !== 'press' && reason !== 'keyboard') return;
    if (initialFocus === false) return;
    const popup = popupRef.current;
    if (!popup) return;
    const target = initialFocus?.current || getTabbables(popup)[0] || popup;
    target.focus({ preventScroll: true });
    // Focus moves once, when the popup appears.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [placed]);

  const handleKeyDown = (event) => {
    onKeyDown?.(event);
    if (event.defaultPrevented || event.key !== 'Tab') return;
    // The popup sits in a portal; continue the Tab order from its trigger.
    const popup = popupRef.current;
    const tabbables = getTabbables(popup);
    const active = document.activeElement;
    const atStart = !tabbables.length || active === popup || active === tabbables[0];
    const atEnd = !tabbables.length || active === tabbables[tabbables.length - 1];
    if (event.shiftKey && atStart) {
      event.preventDefault();
      context.setOpen(false, 'focus-out');
      triggerRef.current?.focus();
    } else if (!event.shiftKey && atEnd) {
      event.preventDefault();
      context.setOpen(false, 'focus-out');
      focusBeside(triggerRef.current, { skip: (node) => popup?.contains(node) });
    }
  };

  const labelledBy = ariaLabel || ariaLabelledBy ? ariaLabelledBy : context.triggerIdRef.current;

  return createPortal(
    <div
      data-slot="popover-positioner"
      data-side={position?.side ?? side}
      style={{
        position: 'fixed',
        top: position?.top ?? 0,
        left: position?.left ?? 0,
        zIndex: POPUP_Z_INDEX,
        visibility: position ? undefined : 'hidden',
        pointerEvents: isPresent ? undefined : 'none',
        '--anchor-width': position ? `${position.anchorWidth}px` : undefined,
        '--available-height': position ? `${position.availableHeight}px` : undefined,
      }}
    >
      <motion.div
        ref={popupRef}
        id={context.popupId}
        role="dialog"
        tabIndex={-1}
        aria-label={ariaLabel}
        aria-labelledby={labelledBy}
        data-slot="popover-content"
        data-side={position?.side ?? side}
        data-align={align}
        data-open={isPresent ? '' : undefined}
        className={cn('ci-popover-popup', className)}
        style={{ ...style, transformOrigin: position?.origin }}
        initial={reduceMotion ? { opacity: 0 } : { opacity: 0, scale: 0.97 }}
        animate={{ opacity: 1, scale: 1 }}
        exit={reduceMotion ? { opacity: 0 } : { opacity: 0, scale: 0.97 }}
        transition={{ duration: POPUP_SECONDS, ease: POPUP_EASE }}
        onKeyDown={handleKeyDown}
        onPointerEnter={(event) => { onPointerEnter?.(event); context.cancelClose(); }}
        onPointerLeave={(event) => {
          onPointerLeave?.(event);
          if (context.hoverRef.current.enabled && event.pointerType !== 'touch') context.scheduleClose();
        }}
        {...props}
      >
        <LayerContext.Provider value={context.layerApi}>{children}</LayerContext.Provider>
      </motion.div>
    </div>,
    document.body,
  );
}

/**
 * The floating panel: role="dialog" (non-modal), rendered in a portal,
 * positioned against the trigger (`side` bottom|top, flips to stay in view;
 * `align` start|center|end), focusable (tabIndex -1). Defaults come from the
 * `.ci-popover-popup` class in index.css (components layer), so any utility
 * in `className` overrides them (p-0, w-80, max-w-..., rounded-...).
 * Name it with `aria-label` or `aria-labelledby` (falls back to the trigger).
 *
 * @param {object} props
 * @param {'start'|'center'|'end'} [props.align='center']
 * @param {'bottom'|'top'} [props.side='bottom']
 * @param {number} [props.sideOffset=4]
 * @param {number} [props.alignOffset=0]
 * @param {import('react').RefObject<HTMLElement>|false} [props.initialFocus] element to focus on press-open; false keeps focus on the trigger
 */
export function PopoverPopup({
  align = 'center', side = 'bottom', sideOffset = 4, alignOffset = 0, ...props
}) {
  const context = usePopoverContext('PopoverPopup');
  return (
    <AnimatePresence>
      {context.open && (
        <PopoverPopupInner
          key="popover-popup"
          context={context}
          align={align}
          side={side}
          sideOffset={sideOffset}
          alignOffset={alignOffset}
          {...props}
        />
      )}
    </AnimatePresence>
  );
}

/** A button inside the popup that closes it and returns focus to the trigger. */
export const PopoverClose = /* @__PURE__ */ forwardRef(function PopoverClose({ onClick, type, ...props }, ref) {
  const context = usePopoverContext('PopoverClose');
  return (
    <button
      ref={ref}
      type={type ?? 'button'}
      data-slot="popover-close"
      onClick={chain(onClick, (event) => {
        if (event.defaultPrevented) return;
        context.setOpen(false, 'trigger-press');
      })}
      {...props}
    />
  );
});

export { PopoverPopup as PopoverContent };
export default Popover;
