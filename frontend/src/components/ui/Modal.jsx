import { X } from 'lucide-react';
import { useEffect, useId, useRef } from 'react';
import { createPortal } from 'react-dom';
import useFocusTrap from '../../hooks/useFocusTrap';

// Nested dialogs share one scroll lock; the last to close restores the page.
let scrollLocks = 0;
let savedBodyStyle = null;

const lockScroll = () => {
  scrollLocks += 1;
  if (scrollLocks > 1) return;
  const { body, documentElement } = document;
  savedBodyStyle = { overflow: body.style.overflow, paddingRight: body.style.paddingRight };
  // Keep the page from shifting sideways when its scrollbar disappears.
  const scrollbar = window.innerWidth - documentElement.clientWidth;
  if (scrollbar > 0) {
    const current = Number.parseFloat(window.getComputedStyle(body).paddingRight) || 0;
    body.style.paddingRight = `${current + scrollbar}px`;
  }
  body.style.overflow = 'hidden';
};

const unlockScroll = () => {
  scrollLocks = Math.max(0, scrollLocks - 1);
  if (scrollLocks > 0 || !savedBodyStyle) return;
  document.body.style.overflow = savedBodyStyle.overflow;
  document.body.style.paddingRight = savedBodyStyle.paddingRight;
  savedBodyStyle = null;
};

/**
 * Accessible dialog: focus is trapped inside, Escape closes (unless busy),
 * focus returns to the invoking element, and the page behind cannot scroll.
 * On phones it is a bottom sheet that clears the home indicator.
 */
const Modal = ({
  open, onClose, title, description, children, footer, size = 'md', busy = false,
  initialFocusRef, className = '', bodyClassName = '', hideHeader = false, labelledBy,
}) => {
  const panelRef = useRef(null);
  const generatedId = useId();
  const titleId = labelledBy || `${generatedId}-title`;
  const descriptionId = `${generatedId}-description`;
  // Callers often pass inline handlers; keep the latest in refs so the effect
  // below runs only when the dialog opens or closes (never on every render,
  // which would steal focus from inputs while the user types).
  const onCloseRef = useRef(onClose);
  const busyRef = useRef(busy);
  onCloseRef.current = onClose;
  busyRef.current = busy;

  useFocusTrap(panelRef, open, {
    initialFocusRef,
    onEscape: () => { if (!busyRef.current) onCloseRef.current?.(); },
  });

  useEffect(() => {
    if (!open) return undefined;
    lockScroll();
    return unlockScroll;
  }, [open]);

  if (!open) return null;
  const width = {
    sm: 'max-w-md', md: 'max-w-xl', lg: 'max-w-3xl', xl: 'max-w-5xl', full: 'max-w-[min(96rem,calc(100vw-2rem))]',
  }[size] || 'max-w-xl';
  const describedBy = !hideHeader && description ? descriptionId : undefined;

  return createPortal(
    <div className="fixed inset-0 z-[80] flex items-end justify-center p-0 sm:items-center sm:p-4" role="presentation">
      <div
        className="absolute inset-0 animate-fade-in bg-black/45 backdrop-blur-[2px] dark:bg-black/65"
        onMouseDown={() => { if (!busy) onClose?.(); }}
        aria-hidden="true"
      />
      <section
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={describedBy}
        tabIndex={-1}
        className={`relative flex max-h-[92dvh] w-full ${width} animate-rise flex-col overflow-hidden rounded-t-3xl border border-line bg-surface pb-[env(safe-area-inset-bottom)] shadow-[var(--shadow-lift)] outline-none dark:border-line-strong sm:rounded-3xl sm:pb-0 ${className}`}
      >
        {!hideHeader && (
          <header className="flex items-start justify-between gap-4 border-b border-line px-5 py-4 sm:px-6">
            <div className="min-w-0">
              <h2 id={titleId} className="text-base font-semibold tracking-tight text-ink">{title}</h2>
              {description && <p id={descriptionId} className="mt-1 text-sm leading-relaxed text-ink-3">{description}</p>}
            </div>
            <button
              type="button"
              onClick={onClose}
              disabled={busy}
              className="-mr-2 -mt-1 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-ink-3 transition-colors duration-(--duration-fast) enabled:hover:bg-surface-2 enabled:hover:text-ink enabled:active:bg-surface-3 disabled:opacity-40 pointer-coarse:h-10 pointer-coarse:w-10"
              aria-label="Close dialog"
            >
              <X className="h-4 w-4" aria-hidden="true" />
            </button>
          </header>
        )}
        <div className={`scroll-thin min-h-0 flex-1 overflow-y-auto overscroll-contain ${bodyClassName || 'px-5 py-5 sm:px-6'}`}>{children}</div>
        {footer && <footer className="flex flex-wrap items-center justify-end gap-2 border-t border-line bg-surface-2/60 px-5 py-3.5 sm:px-6">{footer}</footer>}
      </section>
    </div>,
    document.body,
  );
};

export default Modal;
