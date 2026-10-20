import { useEffect, useId, useRef, useState } from 'react';

const ITEM_SELECTOR = '[role="menuitem"]:not([disabled])';

/**
 * Small dropdown menu. Closes on outside press, Escape, Tab, or selection
 * and supports arrow/Home/End navigation plus first-letter type-ahead.
 */
const Menu = ({ trigger, items, align = 'right', label = 'Actions', className = '' }) => {
  const [open, setOpen] = useState(false);
  const rootRef = useRef(null);
  const listRef = useRef(null);
  const menuId = useId();

  useEffect(() => {
    if (!open) return undefined;
    const focusTrigger = () => rootRef.current?.querySelector('[data-menu-trigger]')?.focus();
    const onPointer = (event) => {
      if (!rootRef.current?.contains(event.target)) setOpen(false);
    };
    const onKey = (event) => {
      const nodes = [...(listRef.current?.querySelectorAll(ITEM_SELECTOR) || [])];
      const index = nodes.indexOf(document.activeElement);
      const focusAt = (next) => {
        event.preventDefault();
        nodes[(next + nodes.length) % nodes.length]?.focus();
      };
      if (event.key === 'Escape') {
        event.preventDefault();
        setOpen(false);
        focusTrigger();
      } else if (event.key === 'Tab') {
        // Let focus move on naturally; just don't leave the menu hanging open.
        setOpen(false);
      } else if (!nodes.length) {
        // Nothing to move between.
      } else if (event.key === 'ArrowDown') {
        focusAt(index + 1);
      } else if (event.key === 'ArrowUp') {
        focusAt(index < 0 ? nodes.length - 1 : index - 1);
      } else if (event.key === 'Home') {
        focusAt(0);
      } else if (event.key === 'End') {
        focusAt(nodes.length - 1);
      } else if (event.key.length === 1 && /\S/.test(event.key) && !event.metaKey && !event.ctrlKey && !event.altKey) {
        // Type-ahead: jump to the next item starting with that letter.
        const letter = event.key.toLowerCase();
        const order = [...nodes.slice(index + 1), ...nodes.slice(0, index + 1)];
        const match = order.find((node) => node.textContent.trim().toLowerCase().startsWith(letter));
        if (match) {
          event.preventDefault();
          match.focus();
        }
      }
    };
    document.addEventListener('pointerdown', onPointer);
    document.addEventListener('keydown', onKey);
    const timer = window.setTimeout(() => listRef.current?.querySelector(ITEM_SELECTOR)?.focus(), 0);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener('pointerdown', onPointer);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  return (
    <div ref={rootRef} className={`relative ${className}`}>
      {trigger({
        open,
        toggle: () => setOpen((value) => !value),
        props: {
          'data-menu-trigger': true,
          'aria-haspopup': 'menu',
          'aria-expanded': open,
          'aria-controls': open ? menuId : undefined,
          'aria-label': label,
        },
      })}
      {open && (
        <div
          ref={listRef}
          id={menuId}
          role="menu"
          aria-label={label}
          className={`absolute z-50 mt-1.5 min-w-52 max-w-[calc(100vw-2rem)] animate-rise overflow-hidden rounded-xl border border-line bg-surface p-1 shadow-[var(--shadow-lift)] dark:border-line-strong ${align === 'right' ? 'right-0 origin-top-right' : 'left-0 origin-top-left'}`}
        >
          {items.filter(Boolean).map((item, index) => item.separator ? (
            <div key={`separator-${index}`} className="my-1 h-px bg-line" role="separator" />
          ) : (
            <button
              key={item.label}
              type="button"
              role="menuitem"
              disabled={item.disabled}
              onClick={() => {
                setOpen(false);
                // Hand focus back to the trigger first: a dialog opened by
                // this item then restores focus there instead of to <body>
                // (the item itself unmounts with the menu).
                rootRef.current?.querySelector('[data-menu-trigger]')?.focus();
                item.onSelect?.();
              }}
              className={`flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[13px] outline-none transition-colors duration-(--duration-fast) pointer-coarse:py-2.5 disabled:opacity-50 focus-visible:ring-2 focus-visible:ring-inset ${item.danger
                ? 'text-danger enabled:hover:bg-danger-soft focus:bg-danger-soft focus-visible:ring-danger/40'
                : 'text-ink-2 enabled:hover:bg-surface-2 enabled:hover:text-ink focus:bg-surface-2 focus:text-ink focus-visible:ring-accent/45'}`}
            >
              {item.icon && <item.icon className="h-4 w-4 shrink-0" aria-hidden="true" />}
              <span className="flex-1">{item.label}</span>
              {item.hint && <span className="text-[11px] text-ink-3">{item.hint}</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
};

export default Menu;

/*
 * The compound menu (MenuRoot, MenuTrigger, MenuPopup, MenuRadioGroup,
 * MenuRadioItem, MenuItem, MenuSeparator, MenuGroupLabel) lives in its own
 * module, so pages that only use the dropdown above (the site header) do not
 * load it and its motion and popover code.
 */
export * from './MenuCompound';
