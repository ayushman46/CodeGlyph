import { BookOpen, CreditCard, LayoutGrid, LogOut, Menu as MenuIcon, PanelLeft, Search, User, X } from 'lucide-react';
import { useEffect, useId, useRef, useState } from 'react';
import { NavLink, useLocation, useNavigate } from 'react-router-dom';
import useStore from '../store/useStore';
import GoogleSignInButton from './GoogleSignInButton';
import StartFreeButton from './landing/StartFreeButton';
import ThemeToggle from './ThemeToggle';
import Kbd from './ui/Kbd';
import Logo from './ui/Logo';
import Menu from './ui/Menu';

const NAV = [
  { to: '/platform', label: 'Platform' },
  { to: '/pricing', label: 'Pricing' },
  { to: '/docs', label: 'Docs' },
];

const navClass = ({ isActive }) => `inline-flex h-9 items-center rounded-lg px-3 text-[13.5px] font-medium transition-colors ${isActive ? 'bg-surface-2 text-ink' : 'text-ink-3 hover:bg-surface-2/70 hover:text-ink'}`;

const mobileNavClass = ({ isActive }) => `flex min-h-11 items-center rounded-lg px-3 text-[15px] font-medium transition-colors ${isActive ? 'bg-surface-2 text-ink' : 'text-ink-2 hover:bg-surface-2 hover:text-ink'}`;

/* Icon buttons are 44px on touch-sized screens and 36px from md up. */
const ICON_BUTTON = 'flex h-11 w-11 shrink-0 items-center justify-center rounded-lg text-ink-3 transition-colors hover:bg-surface-2 hover:text-ink md:h-9 md:w-9';

/* Pages do not share a <main id>, so the skip link focuses whichever <main>
 * the current route renders. */
const skipToContent = (event) => {
  const main = document.querySelector('main');
  if (!main) return;
  event.preventDefault();
  if (!main.hasAttribute('tabindex')) {
    main.setAttribute('tabindex', '-1');
    main.style.outline = 'none';
  }
  main.focus();
};

const Avatar = ({ user, size = 'h-8 w-8' }) => {
  const name = user?.user_metadata?.full_name || user?.email || 'Account';
  const avatar = user?.user_metadata?.avatar_url;
  return avatar ? (
    <img src={avatar} alt="" className={`${size} rounded-full border border-line object-cover`} referrerPolicy="no-referrer" />
  ) : (
    <span className={`${size} flex items-center justify-center rounded-full bg-accent-soft text-[11px] font-semibold uppercase text-accent-strong`}>
      {name.slice(0, 2)}
    </span>
  );
};

const SiteHeader = ({ variant = 'marketing', onToggleSidebar, onOpenPalette }) => {
  const user = useStore((state) => state.user);
  const signOut = useStore((state) => state.signOut);
  const navigate = useNavigate();
  const location = useLocation();
  const [mobileOpen, setMobileOpen] = useState(false);
  const headerRef = useRef(null);
  const toggleRef = useRef(null);
  const panelId = useId();
  const isApp = variant === 'app';

  useEffect(() => setMobileOpen(false), [location.pathname]);

  // The mobile menu is a disclosure: Escape closes it and returns focus to
  // the toggle, focus or a tap leaving the header closes it, and it closes
  // itself if the window grows past the breakpoint where it is hidden.
  useEffect(() => {
    if (!mobileOpen) return undefined;
    const header = headerRef.current;
    const onKey = (event) => {
      if (event.key !== 'Escape') return;
      setMobileOpen(false);
      toggleRef.current?.focus();
    };
    const onFocusOut = (event) => {
      if (event.relatedTarget && !header?.contains(event.relatedTarget)) setMobileOpen(false);
    };
    const wide = window.matchMedia?.('(min-width: 768px)');
    const onWide = (event) => { if (event.matches) setMobileOpen(false); };
    document.addEventListener('keydown', onKey);
    header?.addEventListener('focusout', onFocusOut);
    wide?.addEventListener?.('change', onWide);
    return () => {
      document.removeEventListener('keydown', onKey);
      header?.removeEventListener('focusout', onFocusOut);
      wide?.removeEventListener?.('change', onWide);
    };
  }, [mobileOpen]);

  return (
    <>
      <header ref={headerRef} className="sticky top-0 z-40 border-b border-line/80 bg-canvas/85 backdrop-blur-md supports-[backdrop-filter]:bg-canvas/70">
        <a
          href="#main"
          onClick={skipToContent}
          className="sr-only focus:not-sr-only focus:fixed focus:left-3 focus:top-2.5 focus:z-[60] focus:inline-flex focus:h-9 focus:items-center focus:rounded-lg focus:bg-ink focus:px-3.5 focus:text-[13px] focus:font-medium focus:text-canvas focus:shadow-[var(--shadow-lift)]"
        >
          Skip to content
        </a>
        <nav className={`${isApp ? 'px-3 sm:px-4' : 'content-shell'} flex h-14 items-center gap-2`} aria-label="Main">
          {isApp && onToggleSidebar && (
            <button type="button" onClick={onToggleSidebar} className={`${ICON_BUTTON} -ml-1 lg:hidden`} aria-label="Show repositories">
              <PanelLeft className="h-4 w-4" aria-hidden="true" />
            </button>
          )}
          {/* Below 360px the wordmark would wrap beside the controls; the
              mark alone remains, and the link keeps its accessible name. */}
          <Logo to={user ? '/app' : '/'} className="h-11 whitespace-nowrap md:h-9 max-[359px]:[&>span]:hidden" />

          {!isApp && (
            <div className="ml-5 hidden items-center gap-0.5 md:flex">
              {NAV.map((item) => <NavLink key={item.to} to={item.to} className={navClass}>{item.label}</NavLink>)}
            </div>
          )}

          <div className="ml-auto flex items-center gap-1 md:gap-1.5">
            {isApp && onOpenPalette && (
              <button
                type="button"
                onClick={onOpenPalette}
                className="hidden h-9 items-center gap-2 rounded-lg border border-line bg-surface px-3 text-[13px] text-ink-3 transition-colors hover:border-line-strong hover:text-ink sm:flex"
                aria-label="Open command palette"
              >
                <Search className="h-3.5 w-3.5" aria-hidden="true" />
                <span>Jump to…</span>
                <span className="ml-3 flex gap-0.5"><Kbd>⌘</Kbd><Kbd>K</Kbd></span>
              </button>
            )}
            <ThemeToggle />
            {user ? (
              <>
                {!isApp && (
                  <NavLink to="/app" className="hidden h-9 items-center rounded-lg bg-ink px-3.5 text-[13px] font-medium text-canvas transition-opacity hover:opacity-90 sm:inline-flex">
                    Open workspace
                  </NavLink>
                )}
                <Menu
                  label="Account menu"
                  trigger={({ toggle, props }) => (
                    <button type="button" onClick={toggle} {...props} className="flex h-11 w-11 items-center justify-center rounded-full transition-opacity hover:opacity-80 md:ml-0.5 md:h-9 md:w-9">
                      <Avatar user={user} />
                    </button>
                  )}
                  items={[
                    { label: 'Workspace', icon: LayoutGrid, onSelect: () => navigate('/app') },
                    { label: 'Account & usage', icon: User, onSelect: () => navigate('/account') },
                    { label: 'Pricing', icon: CreditCard, onSelect: () => navigate('/pricing') },
                    { label: 'Documentation', icon: BookOpen, onSelect: () => navigate('/docs') },
                    { separator: true },
                    { label: 'Sign out', icon: LogOut, danger: true, onSelect: async () => { await signOut(); navigate('/'); } },
                  ]}
                />
              </>
            ) : (
              /* 32px tall to match the header rhythm; the pseudo-element
                 extends the touch target to 44px. */
              <GoogleSignInButton compact className="relative after:absolute after:inset-x-0 after:-inset-y-1.5" />
            )}
            {!isApp && (
              <button
                ref={toggleRef}
                type="button"
                onClick={() => setMobileOpen((open) => !open)}
                className={`${ICON_BUTTON} -mr-1.5 md:hidden`}
                aria-label="Toggle navigation"
                aria-expanded={mobileOpen}
                aria-controls={panelId}
              >
                {mobileOpen ? <X className="h-5 w-5" aria-hidden="true" /> : <MenuIcon className="h-5 w-5" aria-hidden="true" />}
              </button>
            )}
          </div>
        </nav>

        {!isApp && mobileOpen && (
          <div id={panelId} className="absolute inset-x-0 top-full animate-fade-in border-b border-line bg-canvas shadow-[var(--shadow-lift)] md:hidden">
            <div className="content-shell flex flex-col gap-0.5 py-3">
              {NAV.map((item) => <NavLink key={item.to} to={item.to} className={mobileNavClass}>{item.label}</NavLink>)}
              <div className="mt-2 border-t border-line pt-3">
                {user ? (
                  <NavLink to="/app" className="flex h-12 w-full items-center justify-center rounded-xl bg-ink text-[15px] font-medium text-canvas transition-opacity hover:opacity-90">
                    Open workspace
                  </NavLink>
                ) : (
                  <StartFreeButton className="w-full" />
                )}
              </div>
            </div>
          </div>
        )}
      </header>

      {/* Outside the header: its backdrop-filter would otherwise become the
          containing block for this fixed scrim. Tapping it closes the menu
          without activating whatever is underneath. */}
      {!isApp && mobileOpen && (
        <div aria-hidden="true" onClick={() => setMobileOpen(false)} className="fixed inset-0 z-30 animate-fade-in bg-ink/25 md:hidden dark:bg-black/50" />
      )}
    </>
  );
};

export { Avatar };
export default SiteHeader;
