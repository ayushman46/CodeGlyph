const ProgressBar = ({ value = 0, tone = 'accent', indeterminate = false, className = '', label }) => {
  const percent = Math.max(0, Math.min(100, Number(value) || 0));
  const color = tone === 'success' ? 'bg-success' : tone === 'danger' ? 'bg-danger' : 'bg-accent';
  return (
    <div
      className={`relative h-1.5 w-full overflow-hidden rounded-full bg-surface-3 ${className}`}
      role="progressbar"
      aria-label={label || 'Progress'}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={indeterminate ? undefined : Math.round(percent)}
      aria-valuetext={indeterminate ? 'In progress' : undefined}
    >
      {indeterminate ? (
        // Under reduced motion this becomes a full-width pulse (index.css).
        <div className={`ci-progress-indeterminate absolute inset-y-0 w-1/3 rounded-full ${color}`} style={{ animation: 'ci-indeterminate 1.4s ease-in-out infinite' }} />
      ) : (
        <div className={`h-full rounded-full ${color} transition-[width] duration-500 ease-(--ease-standard)`} style={{ width: `${percent}%` }} />
      )}
    </div>
  );
};

export default ProgressBar;
