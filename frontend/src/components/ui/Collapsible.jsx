import { useReducedMotion } from 'framer-motion';
import {
  createContext, forwardRef, useCallback, useContext, useId, useLayoutEffect, useMemo, useRef, useState,
} from 'react';
import { cn } from '../../lib/cn';
import { mergeRefs, renderElement } from './Popover';

/*
 * Collapsible (Base UI semantics): a trigger button that shows and hides a
 * panel. The root and the trigger carry `data-panel-open` while open, so a
 * chevron inside a named group can turn with
 *   <CollapsibleTrigger className="group/summary ...">
 *     <ChevronDown className="-rotate-90 group-data-panel-open/summary:rotate-0" />
 * The panel stays mounted (aria-controls always points at it) and is
 * `hidden` while closed; its height glides open and shut (200ms) unless the
 * reader prefers reduced motion or `animated={false}`.
 */

const CollapsibleContext = /* @__PURE__ */ createContext(null);

const useCollapsibleContext = (name) => {
  const context = useContext(CollapsibleContext);
  if (!context) throw new Error(`<${name}> must be used inside <Collapsible>.`);
  return context;
};

/** The reference panel: `transition-[height] duration-200` with Tailwind's default ease. */
const PANEL_MS = 200;
const PANEL_EASE = 'cubic-bezier(0.4, 0, 0.2, 1)';

/**
 * Root <div>. Controlled with `open` + `onOpenChange(open)`, or uncontrolled
 * from `defaultOpen`. `disabled` disables the trigger.
 *
 * @param {object} props
 * @param {boolean} [props.open]
 * @param {boolean} [props.defaultOpen=false]
 * @param {(open: boolean) => void} [props.onOpenChange]
 * @param {boolean} [props.disabled=false]
 */
export const Collapsible = /* @__PURE__ */ forwardRef(function Collapsible({
  open: openProp, defaultOpen = false, onOpenChange, disabled = false, className, children, ...props
}, ref) {
  const isControlled = openProp !== undefined;
  const [uncontrolledOpen, setUncontrolledOpen] = useState(defaultOpen);
  const open = isControlled ? Boolean(openProp) : uncontrolledOpen;
  const panelId = useId();
  const onOpenChangeRef = useRef(onOpenChange);
  onOpenChangeRef.current = onOpenChange;

  const setOpen = useCallback((next) => {
    if (!isControlled) setUncontrolledOpen(next);
    onOpenChangeRef.current?.(next);
  }, [isControlled]);

  const value = useMemo(() => ({ open, setOpen, panelId, disabled }), [open, setOpen, panelId, disabled]);

  return (
    <CollapsibleContext.Provider value={value}>
      <div
        ref={ref}
        data-slot="collapsible"
        data-panel-open={open ? '' : undefined}
        data-open={open ? '' : undefined}
        data-closed={open ? undefined : ''}
        className={className}
        {...props}
      >
        {children}
      </div>
    </CollapsibleContext.Provider>
  );
});

/**
 * The toggle <button> (or `render` element): aria-expanded, aria-controls,
 * `data-panel-open` while open.
 *
 * @param {object} props
 * @param {import('react').ReactElement} [props.render] element to render instead of <button>
 */
export const CollapsibleTrigger = /* @__PURE__ */ forwardRef(function CollapsibleTrigger({
  render, className, children, onClick, disabled: disabledProp, type, ...props
}, ref) {
  const context = useCollapsibleContext('CollapsibleTrigger');
  const disabled = disabledProp ?? context.disabled;
  const triggerProps = {
    ...props,
    className: cn('cursor-pointer', className),
    disabled,
    'aria-expanded': context.open,
    'aria-controls': context.panelId,
    'data-panel-open': context.open ? '' : undefined,
    'data-slot': props['data-slot'] ?? 'collapsible-trigger',
    onClick: (event) => {
      onClick?.(event);
      if (event.defaultPrevented || disabled) return;
      context.setOpen(!context.open);
    },
  };
  if (render) return renderElement(render, { ...triggerProps, children }, ref);
  return (
    <button ref={ref} type={type ?? 'button'} {...triggerProps}>
      {children}
    </button>
  );
});

/**
 * The panel <div>: `hidden` while closed (after its closing glide).
 *
 * @param {object} props
 * @param {boolean} [props.animated=true] glide the height; always off under reduced motion
 */
export const CollapsiblePanel = /* @__PURE__ */ forwardRef(function CollapsiblePanel({
  animated = true, className, style, children, ...props
}, ref) {
  const context = useCollapsibleContext('CollapsiblePanel');
  const reduceMotion = useReducedMotion() ?? false;
  const panelRef = useRef(null);
  const setRef = useMemo(() => mergeRefs(ref, panelRef), [ref]);
  const canAnimate = animated && !reduceMotion;
  // Stays shown while it glides shut: set in the same render that closes it,
  // so the panel is never hidden before its height can be measured.
  const [closing, setClosing] = useState(false);
  const [renderedOpen, setRenderedOpen] = useState(context.open);
  if (renderedOpen !== context.open) {
    setRenderedOpen(context.open);
    setClosing(!context.open && canAnimate);
  }
  const animationRef = useRef(null);
  const lastOpen = useRef(context.open);

  useLayoutEffect(() => {
    // Glide only when the open state changes, never on mount.
    if (lastOpen.current === context.open) return;
    lastOpen.current = context.open;
    const panel = panelRef.current;
    if (!panel) return;
    const running = animationRef.current;
    // Start from where an interrupted glide left off.
    const from = running ? panel.getBoundingClientRect().height : null;
    running?.cancel();
    animationRef.current = null;
    if (!canAnimate || typeof panel.animate !== 'function') {
      setClosing(false);
      return;
    }
    if (context.open) {
      const to = panel.scrollHeight;
      const animation = panel.animate(
        [{ height: `${from ?? 0}px`, overflow: 'hidden' }, { height: `${to}px`, overflow: 'hidden' }],
        { duration: PANEL_MS, easing: PANEL_EASE },
      );
      animationRef.current = animation;
      animation.onfinish = () => { if (animationRef.current === animation) animationRef.current = null; };
    } else {
      const start = from ?? panel.getBoundingClientRect().height;
      const animation = panel.animate(
        [{ height: `${start}px`, overflow: 'hidden' }, { height: '0px', overflow: 'hidden' }],
        { duration: PANEL_MS, easing: PANEL_EASE, fill: 'forwards' },
      );
      animationRef.current = animation;
      animation.onfinish = () => {
        if (animationRef.current !== animation) return;
        animationRef.current = null;
        setClosing(false);
        // Drop the held 0px frame once the panel is hidden.
        window.requestAnimationFrame(() => animation.cancel());
      };
    }
  }, [context.open, canAnimate]);

  const shown = context.open || closing;
  return (
    <div
      ref={setRef}
      id={context.panelId}
      hidden={!shown}
      data-slot="collapsible-panel"
      data-open={context.open ? '' : undefined}
      data-closed={context.open ? undefined : ''}
      className={className}
      style={style}
      {...props}
    >
      {children}
    </div>
  );
});

export { CollapsiblePanel as CollapsibleContent };
export default Collapsible;
