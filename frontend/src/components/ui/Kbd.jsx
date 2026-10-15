const Kbd = ({ children, className = '' }) => (
  <kbd className={`inline-flex h-5 min-w-5 items-center justify-center rounded border border-line bg-surface-2 px-1 font-sans text-[10px] font-medium text-ink-3 ${className}`}>
    {children}
  </kbd>
);

export default Kbd;
