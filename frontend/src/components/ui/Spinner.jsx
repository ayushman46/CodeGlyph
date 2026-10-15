const Spinner = ({ className = 'h-4 w-4', label }) => (
  <svg className={`animate-spin ${className}`} viewBox="0 0 24 24" fill="none" role={label ? 'status' : undefined} aria-label={label} aria-hidden={label ? undefined : true}>
    <circle cx="12" cy="12" r="9.5" stroke="currentColor" strokeOpacity="0.2" strokeWidth="3" />
    <path d="M21.5 12a9.5 9.5 0 0 0-9.5-9.5" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
  </svg>
);

export default Spinner;
