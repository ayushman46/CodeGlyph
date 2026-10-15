import { useEffect, useRef } from 'react';

export const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

// Open traps, innermost last: only the top one handles Tab and Escape, so a
// command palette opened over the review dialog does not fight it for focus.
const stack = [];

const focusableIn = (container) => [...container.querySelectorAll(FOCUSABLE)]
  .filter((node) => node.offsetParent !== null || node === document.activeElement);

/**
 * The element focus should return to when a surface opened from `element`
 * closes. A menu item unmounts with its menu, so fall back to that menu's
 * trigger button.
 */
const restoreTarget = (element) => {
  if (!element || element === document.body) return null;
  const menu = element.closest?.('[role="menu"]');
  const trigger = menu?.parentElement?.querySelector?.('[data-menu-trigger]');
  return { element, fallback: trigger || null };
};

/**
 * Modal focus management for `containerRef` while `active`: focus moves
 * inside on open, Tab/Shift+Tab cycle within it, Escape calls `onEscape`
 * from anywhere, and focus returns to the opener (or its menu trigger) on close.
 */
const useFocusTrap = (containerRef, active, { onEscape, initialFocusRef, restoreFocus = true } = {}) => {
  const onEscapeRef = useRef(onEscape);
  onEscapeRef.current = onEscape;
  const initialRef = useRef(initialFocusRef);
  initialRef.current = initialFocusRef;

  useEffect(() => {
    if (!active) return undefined;
    const token = {};
    stack.push(token);
    const opener = restoreTarget(document.activeElement);
    const focusTimer = window.setTimeout(() => {
      const container = containerRef.current;
      if (!container || container.contains(document.activeElement)) return;
      const target = initialRef.current?.current || focusableIn(container)[0] || container;
      target?.focus?.();
    }, 0);
    const onKeyDown = (event) => {
      if (stack[stack.length - 1] !== token) return;
      const container = containerRef.current;
      if (!container) return;
      if (event.key === 'Escape') {
        if (!onEscapeRef.current) return;
        event.stopPropagation();
        event.preventDefault();
        onEscapeRef.current(event);
        return;
      }
      if (event.key !== 'Tab') return;
      const focusable = focusableIn(container);
      if (!focusable.length) {
        event.preventDefault();
        container.focus?.();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const inside = container.contains(document.activeElement);
      if (!inside) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      } else if (event.shiftKey && (document.activeElement === first || document.activeElement === container)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', onKeyDown, true);
    return () => {
      window.clearTimeout(focusTimer);
      document.removeEventListener('keydown', onKeyDown, true);
      const index = stack.indexOf(token);
      if (index >= 0) stack.splice(index, 1);
      if (!restoreFocus || !opener) return;
      // Restore after React has committed the close (the opener may re-render).
      window.setTimeout(() => {
        const target = opener.element.isConnected ? opener.element : opener.fallback;
        const current = document.activeElement;
        if (target?.isConnected && (!current || current === document.body || !current.isConnected)) {
          target.focus?.();
        }
      }, 0);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [active]);
};

export default useFocusTrap;
