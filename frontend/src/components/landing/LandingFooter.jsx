import { Link } from 'react-router-dom';
import { BrandGlyph } from './MockParts';

const LINKS = [
  { to: '/platform', label: 'Platform' },
  { to: '/pricing', label: 'Pricing' },
  { to: '/docs', label: 'Docs' },
];

const LandingFooter = () => (
  <footer className="border-t border-line">
    <div className="content-shell flex flex-col gap-3 pb-[max(1.5rem,env(safe-area-inset-bottom))] pt-8 sm:flex-row sm:items-center sm:justify-between sm:pb-8">
      <p className="flex items-center gap-2.5 text-[13px] text-ink-2">
        <BrandGlyph className="h-6 w-6 shrink-0" />
        <span>&copy; {new Date().getFullYear()} Codebase Intel</span>
      </p>
      <nav aria-label="Footer">
        <ul className="-mx-3 flex flex-wrap items-center">
          {LINKS.map((link) => (
            <li key={link.to}>
              <Link to={link.to} className="inline-flex min-h-11 items-center rounded-lg px-3 text-[13px] font-medium text-ink-2 transition-colors hover:bg-surface-2 hover:text-ink sm:min-h-9">
                {link.label}
              </Link>
            </li>
          ))}
        </ul>
      </nav>
    </div>
  </footer>
);

export default LandingFooter;
