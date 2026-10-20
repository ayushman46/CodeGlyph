import { AnimatePresence, motion, useIsPresent, useReducedMotion } from 'framer-motion';
import { Check } from 'lucide-react';
import {
  createContext, forwardRef, useCallback, useContext, useEffect, useId, useMemo, useRef, useState,
} from 'react';
import { createPortal } from 'react-dom';
import { cn } from '../../lib/cn';
import {
  LayerContext, POPUP_EASE, focusBeside, mergeRefs, renderElement, useAnchoredPosition, useDismissableLayer,
} from './Popover';

/* ------------------------------------------------------------------ */
/* Compound menu (Base UI Menu semantics, used by the Honest UI ports) */
/*   <MenuRoot>                                                        */
/*     <MenuTrigger render={<Button variant="link" size="sm" />}>…     */
/*     <MenuPopup align="start">                                       */
/*       <MenuRadioGroup value={v} onValueChange={setV}>               */
/*         <MenuRadioItem value="a">…</MenuRadioItem>                  */
/* Re-exported by Menu.jsx, whose default export (the trigger/items    */
/* dropdown) is unchanged: import these from '../ui/Menu'.             */
/* ------------------------------------------------------------------ */

const MenuContext = /* @__PURE__ */ createContext(null);
const RadioGroupContext = /* @__PURE__ */ createContext(null);

const useMenuContext = (name) => {
  const context = useContext(MenuContext);
  if (!context) throw new Error(`<${name}> must be used inside <MenuRoot>.`);
  return context;
};

const MENU_ITEM_SELECTOR = '[role="menuitem"],[role="menuitemradio"],[role="menuitemcheckbox"]';
const MENU_SECONDS = 0.15; // --hui-duration-fast

const menuItems = (popup) => [...(popup?.querySelectorAll(MENU_ITEM_SELECTOR) || [])]
  .filter((node) => !node.disabled && node.getAttribute('aria-disabled') !== 'true');

// Shared by every item; the display (flex or grid) is added per item type.
const ITEM_CLASS = [
  'relative w-full cursor-pointer gap-2 rounded-lg px-2 py-2 text-left text-[13px] leading-4 text-ink',
  'outline-none select-none transition-colors duration-(--duration-fast) pointer-coarse:py-2.5',
  'focus:bg-surface-2 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/45',
  'disabled:cursor-not-allowed disabled:text-ink-3',
  "[&_svg]:pointer-events-none [&_svg]:shrink-0 [&_svg]:text-ink-2 [&_svg:not([class*='size-'])]:size-4",
].join(' ');

/**
 * Root of a compound menu. Controlled (`open` + `onOpenChange`) or
 * uncontrolled (`defaultOpen`). Modal by default like Base UI: the press
 * that dismisses it does not also click what is under it.
 *
 * @param {object} props
 * @param {boolean} [props.open]
 * @param {boolean} [props.defaultOpen=false]
 * @param {(open: boolean, details: { reason: string }) => void} [props.onOpenChange]
 * @param {boolean} [props.modal=true]
 */
export function MenuRoot({ open: openProp, defaultOpen = false, onOpenChange, modal = true, children }) {
  const isControlled = openProp !== undefined;
  const [uncontrolledOpen, setUncontrolledOpen] = useState(defaultOpen);
  const open = isControlled ? Boolean(openProp) : uncontrolledOpen;
  const triggerRef = useRef(null);
  const popupRef = useRef(null);
  const triggerIdRef = useRef(undefined);
  const popupId = useId();
  // Which item takes focus when the menu opens: 'auto' (checked, else first) | 'last'.
  const focusIntentRef = useRef('auto');
  const openRef = useRef(open);
  openRef.current = open;
  const onOpenChangeRef = useRef(onOpenChange);
  onOpenChangeRef.current = onOpenChange;
  const controlledRef = useRef(isControlled);
  controlledRef.current = isControlled;

  const setOpen = useCallback((next, reason, intent = 'auto') => {
    if (next === openRef.current) return;
    if (next) focusIntentRef.current = intent;
    openRef.current = next;
    if (!controlledRef.current) setUncontrolledOpen(next);
    onOpenChangeRef.current?.(next, { reason });
  }, []);

  const layerApi = useDismissableLayer({
    open,
    popupRef,
    triggerRef,
    getReason: () => 'press',
    consumeOutsidePress: modal,
    onDismiss: (reason) => setOpen(false, reason === 'escape' ? 'escape-key' : reason),
  });

  const value = useMemo(() => ({
    open, setOpen, triggerRef, popupRef, triggerIdRef, popupId, focusIntentRef, layerApi,
  }), [open, setOpen, popupId, layerApi]);

  return <MenuContext.Provider value={value}>{children}</MenuContext.Provider>;
}

/**
 * The menu button: aria-haspopup="menu", aria-expanded, aria-controls
 * (while open), `data-popup-open` (while open). Press, Enter, Space or
 * ArrowDown open it on the checked (else first) item; ArrowUp on the last.
 *
 * @param {object} props
 * @param {import('react').ReactElement} [props.render] element to render instead of <button>, e.g. <Button variant="link" size="sm" />
 */
export const MenuTrigger = /* @__PURE__ */ forwardRef(function MenuTrigger({
  render, className, children, disabled, type, onClick, onKeyDown, id, ...props
}, ref) {
  const context = useMenuContext('MenuTrigger');
  const generatedId = useId();
  const triggerId = id || generatedId;
  context.triggerIdRef.current = triggerId;
  const setRef = useMemo(() => mergeRefs(ref, context.triggerRef), [ref, context.triggerRef]);

  const triggerProps = {
    ...props,
    id: triggerId,
    className,
    disabled,
    'aria-haspopup': 'menu',
    'aria-expanded': context.open,
    'aria-controls': context.open ? context.popupId : undefined,
    'data-popup-open': context.open ? '' : undefined,
    'data-slot': props['data-slot'] ?? 'menu-trigger',
    onClick: (event) => {
      onClick?.(event);
      if (event.defaultPrevented || disabled) return;
      context.setOpen(!context.open, 'trigger-press');
    },
    onKeyDown: (event) => {
      onKeyDown?.(event);
      if (event.defaultPrevented || disabled) return;
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        context.setOpen(true, 'trigger-press', event.key === 'ArrowUp' ? 'last' : 'auto');
      }
    },
  };
  if (render) return renderElement(render, { ...triggerProps, children }, setRef);
  return (
    <button ref={setRef} type={type ?? 'button'} {...triggerProps}>
      {children}
    </button>
  );
});

function MenuPopupInner({
  context, align, side, sideOffset, alignOffset, className, style, children, onKeyDown, ...props
}) {
  const reduceMotion = useReducedMotion() ?? false;
  const isPresent = useIsPresent();
  const { popupRef, triggerRef } = context;
  const position = useAnchoredPosition(triggerRef, popupRef, { side, align, sideOffset, alignOffset });
  const typeahead = useRef({ text: '', timer: 0 });
  useEffect(() => () => window.clearTimeout(typeahead.current.timer), []);

  // Focus moves once, when the menu appears (after its first position: a
  // still-hidden menu cannot take focus).
  const placed = position !== null;
  const focusedRef = useRef(false);
  useEffect(() => {
    if (!placed || focusedRef.current) return;
    focusedRef.current = true;
    const popup = popupRef.current;
    const items = menuItems(popup);
    const target = context.focusIntentRef.current === 'last'
      ? items[items.length - 1]
      : items.find((item) => item.getAttribute('aria-checked') === 'true') || items[0];
    (target || popup)?.focus({ preventScroll: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [placed]);

  const handleKeyDown = (event) => {
    onKeyDown?.(event);
    if (event.defaultPrevented) return;
    const popup = popupRef.current;
    const items = menuItems(popup);
    const index = items.indexOf(document.activeElement);
    const focusAt = (next) => {
      event.preventDefault();
      items[(next + items.length) % items.length]?.focus({ preventScroll: false });
    };
    if (event.key === 'Tab') {
      event.preventDefault();
      context.setOpen(false, 'focus-out');
      if (event.shiftKey) triggerRef.current?.focus();
      else focusBeside(triggerRef.current, { skip: (node) => popup?.contains(node) });
      return;
    }
    if (!items.length) return;
    if (event.key === 'ArrowDown') focusAt(index < 0 ? 0 : index + 1);
    else if (event.key === 'ArrowUp') focusAt(index < 0 ? items.length - 1 : index - 1);
    else if (event.key === 'Home') focusAt(0);
    else if (event.key === 'End') focusAt(items.length - 1);
    else if (event.key.length === 1 && /\S/.test(event.key) && !event.metaKey && !event.ctrlKey && !event.altKey) {
      // Type-ahead over the items' text, like the default Menu above.
      window.clearTimeout(typeahead.current.timer);
      typeahead.current.text += event.key.toLowerCase();
      typeahead.current.timer = window.setTimeout(() => { typeahead.current.text = ''; }, 500);
      const query = typeahead.current.text;
      const order = query.length === 1 ? [...items.slice(index + 1), ...items.slice(0, index + 1)] : [...items.slice(Math.max(index, 0)), ...items.slice(0, Math.max(index, 0))];
      const match = order.find((node) => node.textContent.trim().toLowerCase().startsWith(query));
      if (match) {
        event.preventDefault();
        match.focus();
      }
    }
  };

  return createPortal(
    <div
      data-slot="menu-positioner"
      data-side={position?.side ?? side}
      style={{
        position: 'fixed',
        top: position?.top ?? 0,
        left: position?.left ?? 0,
        zIndex: 90,
        visibility: position ? undefined : 'hidden',
        pointerEvents: isPresent ? undefined : 'none',
        '--anchor-width': position ? `${position.anchorWidth}px` : undefined,
        '--available-height': position ? `${position.availableHeight}px` : undefined,
      }}
    >
      <motion.div
        ref={popupRef}
        id={context.popupId}
        role="menu"
        tabIndex={-1}
        aria-labelledby={props['aria-label'] ? undefined : context.triggerIdRef.current}
        data-slot="menu-popup"
        data-side={position?.side ?? side}
        data-align={align}
        className={cn('ci-menu-popup', className)}
        style={{ ...style, transformOrigin: position?.origin }}
        initial={reduceMotion ? { opacity: 0 } : { opacity: 0, scale: 0.97 }}
        animate={{ opacity: 1, scale: 1 }}
        exit={reduceMotion ? { opacity: 0 } : { opacity: 0, scale: 0.97 }}
        transition={{ duration: MENU_SECONDS, ease: POPUP_EASE }}
        onKeyDown={handleKeyDown}
        // The highlight leaves with the pointer; keyboard focus stays in the menu.
        onPointerLeave={(event) => {
          if (event.pointerType !== 'touch' && popupRef.current?.contains(document.activeElement)) {
            popupRef.current.focus({ preventScroll: true });
          }
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
 * The menu list (role="menu") in a portal, positioned against the trigger
 * (`side` bottom|top with flip, `align` start|center|end), at least as wide
 * as the trigger. ArrowUp/ArrowDown (wrapping), Home/End and type-ahead
 * move between items; Escape closes and returns focus to the trigger; Tab
 * closes and continues from the trigger. Defaults come from `.ci-menu-popup`
 * (index.css, components layer), so `className` utilities override them.
 *
 * @param {object} props
 * @param {'start'|'center'|'end'} [props.align='center']
 * @param {'bottom'|'top'} [props.side='bottom']
 * @param {number} [props.sideOffset=4]
 * @param {number} [props.alignOffset=0]
 */
export function MenuPopup({ align = 'center', side = 'bottom', sideOffset = 4, alignOffset = 0, ...props }) {
  const context = useMenuContext('MenuPopup');
  return (
    <AnimatePresence>
      {context.open && (
        <MenuPopupInner
          key="menu-popup"
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

const useItemPointerFocus = () => useCallback((event) => {
  // The highlight follows the pointer (a hovered item is the focused one).
  if (event.pointerType === 'touch' || event.currentTarget.disabled) return;
  if (document.activeElement !== event.currentTarget) event.currentTarget.focus({ preventScroll: true });
}, []);

/**
 * A plain action (role="menuitem"). Closes the menu when pressed unless
 * `closeOnClick` is false; focus returns to the trigger.
 */
export const MenuItem = /* @__PURE__ */ forwardRef(function MenuItem({
  closeOnClick = true, disabled, className, children, onClick, onPointerMove, ...props
}, ref) {
  const context = useMenuContext('MenuItem');
  const focusOnPointer = useItemPointerFocus();
  return (
    <button
      ref={ref}
      type="button"
      role="menuitem"
      tabIndex={-1}
      disabled={disabled}
      data-slot="menu-item"
      data-disabled={disabled ? '' : undefined}
      className={cn('flex items-center', ITEM_CLASS, className)}
      onPointerMove={(event) => { onPointerMove?.(event); focusOnPointer(event); }}
      onClick={(event) => {
        onClick?.(event);
        if (event.defaultPrevented || disabled || !closeOnClick) return;
        context.setOpen(false, 'item-press');
      }}
      {...props}
    >
      {children}
    </button>
  );
});

/**
 * A group of mutually exclusive items (role="group"). Controlled with
 * `value` + `onValueChange(value)`, or uncontrolled from `defaultValue`.
 */
export const MenuRadioGroup = /* @__PURE__ */ forwardRef(function MenuRadioGroup({
  value: valueProp, defaultValue, onValueChange, disabled = false, className, children, ...props
}, ref) {
  const isControlled = valueProp !== undefined;
  const [uncontrolledValue, setUncontrolledValue] = useState(defaultValue);
  const value = isControlled ? valueProp : uncontrolledValue;
  const onValueChangeRef = useRef(onValueChange);
  onValueChangeRef.current = onValueChange;
  const setValue = useCallback((next) => {
    if (!isControlled) setUncontrolledValue(next);
    onValueChangeRef.current?.(next);
  }, [isControlled]);
  const contextValue = useMemo(() => ({ value, setValue, disabled }), [value, setValue, disabled]);
  return (
    <RadioGroupContext.Provider value={contextValue}>
      <div ref={ref} role="group" data-slot="menu-radio-group" className={className} {...props}>
        {children}
      </div>
    </RadioGroupContext.Provider>
  );
});

/**
 * One choice in a MenuRadioGroup (role="menuitemradio", aria-checked), with
 * a check in a 1rem leading column while chosen. Like Base UI, choosing
 * keeps the menu open unless `closeOnClick` is set.
 */
export const MenuRadioItem = /* @__PURE__ */ forwardRef(function MenuRadioItem({
  value, closeOnClick = false, disabled: disabledProp, className, children, onClick, onPointerMove, ...props
}, ref) {
  const context = useMenuContext('MenuRadioItem');
  const group = useContext(RadioGroupContext);
  if (!group) throw new Error('<MenuRadioItem> must be used inside <MenuRadioGroup>.');
  const focusOnPointer = useItemPointerFocus();
  const checked = Object.is(group.value, value);
  const disabled = disabledProp ?? group.disabled;
  return (
    <button
      ref={ref}
      type="button"
      role="menuitemradio"
      aria-checked={checked}
      tabIndex={-1}
      disabled={disabled}
      data-slot="menu-radio-item"
      data-checked={checked ? '' : undefined}
      data-disabled={disabled ? '' : undefined}
      className={cn('grid grid-cols-[1rem_1fr] items-center', ITEM_CLASS, className)}
      onPointerMove={(event) => { onPointerMove?.(event); focusOnPointer(event); }}
      onClick={(event) => {
        onClick?.(event);
        if (event.defaultPrevented || disabled) return;
        if (!checked) group.setValue(value);
        if (closeOnClick) context.setOpen(false, 'item-press');
      }}
      {...props}
    >
      <span aria-hidden="true" data-slot="menu-radio-item-indicator" className="col-start-1 flex size-4 items-center justify-center">
        {checked && <Check />}
      </span>
      <span className="col-start-2 min-w-0">{children}</span>
    </button>
  );
});

/** A hairline between groups of items. */
export const MenuSeparator = ({ className, ...props }) => (
  <div role="separator" data-slot="menu-separator" className={cn('-mx-1 my-1 h-px bg-line', className)} {...props} />
);

/** A small heading above a group of items (not focusable). */
export const MenuGroupLabel = ({ className, ...props }) => (
  <div data-slot="menu-label" className={cn('px-2 py-1 text-[11px] leading-3.5 font-medium text-ink-3', className)} {...props} />
);
