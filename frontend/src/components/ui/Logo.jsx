import { useId } from 'react';
import { Link } from 'react-router-dom';

export const LogoMark = ({ className = 'h-7 w-7' }) => {
  // A unique gradient id per mark: with a shared id, every logo on the page
  // resolves to the first one, and if that copy is inside a hidden menu the
  // gradient stops painting everywhere.
  const gradientId = `ci-logo-gradient-${useId().replace(/:/g, '')}`;
  return (
    <svg className={className} viewBox="0 0 32 32" fill="none" aria-hidden="true">
      <rect width="32" height="32" rx="9" fill={`url(#${gradientId})`} />
      <path d="M12.5 10.5 7.5 16l5 5.5M19.5 10.5l5 5.5-5 5.5" stroke="#fff" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="16" cy="16" r="1.9" fill="#fff" />
      <defs>
        <linearGradient id={gradientId} x1="0" y1="0" x2="32" y2="32" gradientUnits="userSpaceOnUse">
          <stop stopColor="#ff7a3d" />
          <stop offset="1" stopColor="#c2410c" />
        </linearGradient>
      </defs>
    </svg>
  );
};

const Logo = ({ to = '/', compact = false, className = '' }) => (
  <Link to={to} className={`inline-flex items-center gap-2.5 rounded-lg transition-opacity duration-(--duration-fast) hover:opacity-85 ${className}`} aria-label="Codebase Intel home">
    <LogoMark />
    {!compact && <span className="text-[15px] font-semibold tracking-tight text-ink">Codebase Intel</span>}
  </Link>
);

export default Logo;
