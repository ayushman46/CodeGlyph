import { useRef } from 'react';

const NEXT_KEYS = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };

/**
 * Single-choice segmented control with radio-group semantics: one tab stop
 * for the whole group, arrow keys (and Home/End) move and select.
 */
const Segmented = ({ options, value, onChange, size = 'sm', label, className = '' }) => {
  const groupRef = useRef(null);
  const enabled = options.filter((option) => !option.disabled);
  const tabbable = enabled.some((option) => option.value === value) ? value : enabled[0]?.value;

  const select = (option) => {
    if (!option) return;
    if (option.value !== value) onChange(option.value);
    const index = options.indexOf(option);
    groupRef.current?.querySelectorAll('[role="radio"]')[index]?.focus();
  };

  const onKeyDown = (event) => {
    if (!enabled.length) return;
    const current = enabled.findIndex((option) => option.value === value);
    let target = null;
    if (event.key in NEXT_KEYS) {
      const step = NEXT_KEYS[event.key];
      const from = current < 0 ? (step > 0 ? -1 : 0) : current;
      target = enabled[(from + step + enabled.length) % enabled.length];
    } else if (event.key === 'Home') {
      target = enabled[0];
    } else if (event.key === 'End') {
      target = enabled[enabled.length - 1];
    }
    if (!target) return;
    event.preventDefault();
    select(target);
  };

  const sizing = size === 'xs'
    ? 'h-6 px-2 text-[11px] pointer-coarse:h-8 pointer-coarse:px-2.5'
    : 'h-7 px-2.5 text-xs pointer-coarse:h-9 pointer-coarse:px-3';

  return (
    <div ref={groupRef} role="radiogroup" aria-label={label} onKeyDown={onKeyDown} className={`inline-flex rounded-xl border border-line bg-surface-2 p-0.5 ${className}`}>
      {options.map((option) => {
        const active = option.value === value;
        const Icon = option.icon;
        return (
          <button
            key={option.value}
            type="button"
            role="radio"
            aria-checked={active}
            tabIndex={option.value === tabbable ? 0 : -1}
            disabled={option.disabled}
            title={option.title}
            onClick={() => onChange(option.value)}
            className={`inline-flex items-center gap-1.5 rounded-[10px] font-medium transition-[background-color,color,box-shadow] duration-(--duration-fast) disabled:opacity-50 ${sizing} ${active ? 'bg-surface text-ink shadow-sm ring-1 ring-line dark:bg-surface-3 dark:ring-line-strong' : 'text-ink-3 enabled:hover:bg-surface/60 enabled:hover:text-ink enabled:active:bg-surface dark:enabled:hover:bg-surface-3/60'}`}
          >
            {Icon && <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />}
            {option.label}
          </button>
        );
      })}
    </div>
  );
};

export default Segmented;
