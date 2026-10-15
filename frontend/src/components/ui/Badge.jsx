const TONES = {
  neutral: 'bg-surface-2 text-ink-2 border-line',
  accent: 'bg-accent-soft text-accent-strong border-transparent',
  success: 'bg-success-soft text-success border-transparent',
  warning: 'bg-warning-soft text-warning border-transparent',
  danger: 'bg-danger-soft text-danger border-transparent',
  info: 'bg-info-soft text-info border-transparent',
};

const Badge = ({ tone = 'neutral', icon: Icon, dot = false, className = '', children, ...props }) => (
  <span
    className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium leading-4 whitespace-nowrap ${TONES[tone] || TONES.neutral} ${className}`}
    {...props}
  >
    {dot && <span className="h-1.5 w-1.5 rounded-full bg-current" aria-hidden="true" />}
    {Icon && <Icon className="h-3 w-3" aria-hidden="true" />}
    {children}
  </span>
);

export default Badge;
