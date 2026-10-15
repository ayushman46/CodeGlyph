const EmptyState = ({ icon: Icon, title, description, action, className = '' }) => (
  <div className={`flex flex-col items-center justify-center rounded-2xl border border-dashed border-line px-6 py-12 text-center ${className}`}>
    {Icon && (
      <div className="mb-4 flex h-11 w-11 items-center justify-center rounded-xl bg-surface-2 text-ink-3">
        <Icon className="h-5 w-5" aria-hidden="true" />
      </div>
    )}
    <p className="text-sm font-semibold text-ink">{title}</p>
    {description && <p className="mt-1.5 max-w-sm text-sm leading-relaxed text-ink-3">{description}</p>}
    {action && <div className="mt-5">{action}</div>}
  </div>
);

export default EmptyState;
