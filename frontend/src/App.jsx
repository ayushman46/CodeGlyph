import { lazy, Suspense, useEffect, useLayoutEffect, useState } from 'react';
import { Link, matchPath, Navigate, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { ArrowRight, Compass } from 'lucide-react';
import { supabase } from './api/supabase';
import RequireAuth from './components/RequireAuth';
import ServerStatus from './components/ServerStatus';
import SiteHeader from './components/SiteHeader';
import Button from './components/ui/Button';
import ErrorBoundary from './components/ui/ErrorBoundary';
import { LogoMark } from './components/ui/Logo';
import Skeleton, { SkeletonText } from './components/ui/Skeleton';
import Spinner from './components/ui/Spinner';
import Toaster from './components/ui/Toast';
import { formatTitle } from './hooks/useDocumentTitle';
import { useRouteFocus, useScrollRestoration } from './hooks/useRouteChange';
import { applyTheme } from './lib/theme';
import useStore, { POST_LOGIN_KEY } from './store/useStore';

// A repository link opened directly (a bookmark, a reload) starts loading its
// history and overview with the app, alongside the workspace's code, instead
// of waiting for that code to download and run first.
const bootRepo = typeof window !== 'undefined' ? matchPath('/app/r/:repoName', window.location.pathname)?.params.repoName : null;
if (bootRepo) {
  supabase.auth.getSession()
    .then(({ data }) => { if (data?.session) useStore.getState().prefetchRepo(decodeURIComponent(bootRepo)); })
    .catch(() => {});
}

// A failed chunk download (flaky mobile network) gets one quiet retry before
// the error boundary takes over (which reloads once for a stale deploy).
const lazyPage = (load) => lazy(() => load().catch(() => new Promise((resolve) => {
  window.setTimeout(resolve, 800);
}).then(load)));

const AccountPage = lazyPage(() => import('./pages/AccountPage'));
const DashboardPage = lazyPage(() => import('./pages/DashboardPage'));
const DocsPage = lazyPage(() => import('./pages/DocsPage'));
const GithubCallbackPage = lazyPage(() => import('./pages/GithubCallbackPage'));
const LandingPage = lazyPage(() => import('./pages/LandingPage'));
const PlatformPage = lazyPage(() => import('./pages/PlatformPage'));
const PricingPage = lazyPage(() => import('./pages/PricingPage'));

const isWorkspacePath = (pathname) => pathname === '/app' || pathname.startsWith('/app/');

/* ---------- Loading states (held back briefly so fast loads never flash) ---------- */

const PageLoader = ({ fullscreen = false, label = 'Loading' }) => (
  <div className={`flex items-center justify-center text-ink-3 ${fullscreen ? 'h-dvh' : 'min-h-[60vh]'}`} role="status" aria-label={label}>
    <div className="ci-delayed-reveal" style={{ '--reveal-delay': '300ms' }}>
      <Spinner className="h-5 w-5" />
    </div>
  </div>
);

/** Marketing page placeholder: the shape of a hero, under the real header. */
const PageSkeleton = () => (
  <div role="status" aria-label="Loading page" className="content-shell flex min-h-[75vh] flex-col items-center pb-16 pt-16 sm:pt-24">
    <div className="ci-delayed-reveal flex w-full max-w-2xl flex-col items-center">
      <Skeleton className="h-3 w-28" />
      <Skeleton className="mt-7 h-9 w-[88%] rounded-xl sm:h-14" />
      <Skeleton className="mt-3 h-9 w-[64%] rounded-xl sm:h-14" />
      <SkeletonText lines={3} className="mt-8 w-full max-w-lg" />
      <div className="mt-9 flex gap-3">
        <Skeleton className="h-12 w-36 rounded-xl" />
        <Skeleton className="h-12 w-28 rounded-xl" />
      </div>
    </div>
  </div>
);

/** Workspace placeholder that matches the app shell, so the real one drops in without a jump. */
const WorkspaceSkeleton = () => (
  <div role="status" aria-label="Loading workspace" className="flex h-dvh flex-col overflow-hidden bg-canvas text-ink">
    <div className="flex h-14 shrink-0 items-center gap-2.5 border-b border-line px-3 sm:px-4">
      <LogoMark className="h-7 w-7" />
      <span className="text-[15px] font-semibold tracking-tight text-ink">Codebase Intel</span>
      <div className="ci-delayed-reveal ml-auto flex items-center gap-2">
        <Skeleton className="hidden h-9 w-40 rounded-lg sm:block" />
        <Skeleton className="h-8 w-8 rounded-full" />
      </div>
    </div>
    <div className="flex min-h-0 flex-1">
      <div className="hidden w-72 shrink-0 border-r border-line bg-surface-2/40 p-3 lg:block">
        <div className="ci-delayed-reveal">
          <Skeleton className="h-11 rounded-2xl" />
          <Skeleton className="ml-1 mt-6 h-2.5 w-24" />
          {[0, 1, 2].map((row) => (
            <div key={row} className="mt-4 flex items-center gap-3 px-1">
              <Skeleton className="h-2 w-2 rounded-full" />
              <div className="flex-1 space-y-2">
                <Skeleton className="h-3 w-3/5" />
                <Skeleton className="h-2.5 w-2/5" />
              </div>
            </div>
          ))}
        </div>
      </div>
      <div className="ci-delayed-reveal flex min-w-0 flex-1 flex-col">
        <div className="mx-auto w-full max-w-3xl flex-1 px-4 pt-8 sm:px-8">
          <Skeleton className="h-5 w-44" />
          <Skeleton className="mt-3 h-3 w-64 max-w-full" />
          <SkeletonText lines={4} className="mt-10" />
          <SkeletonText lines={3} className="mt-8" />
        </div>
        <div className="mx-auto w-full max-w-3xl px-4 pb-5 sm:px-8">
          <Skeleton className="h-24 rounded-2xl" />
        </div>
      </div>
    </div>
  </div>
);

/* ---------- Error and 404 ---------- */

const RouteBoundary = ({ children, fallback, fullscreen = false }) => {
  const { pathname } = useLocation();
  // Navigating anywhere else clears a crashed page.
  return (
    <ErrorBoundary resetKeys={[pathname]} fullscreen={fullscreen}>
      <Suspense fallback={fallback}>{children}</Suspense>
    </ErrorBoundary>
  );
};

const KNOWN_SECTIONS = ['platform', 'pricing', 'docs', 'account', 'app'];

const editDistance = (a, b) => {
  const row = Array.from({ length: b.length + 1 }, (_, index) => index);
  for (let i = 1; i <= a.length; i += 1) {
    let previous = row[0];
    row[0] = i;
    for (let j = 1; j <= b.length; j += 1) {
      const saved = row[j];
      row[j] = Math.min(row[j] + 1, row[j - 1] + 1, previous + (a[i - 1] === b[j - 1] ? 0 : 1));
      previous = saved;
    }
  }
  return row[b.length];
};

/** Best guess at where a broken link meant to go, or null. */
const suggestPath = (pathname) => {
  const segments = pathname.split('/').filter(Boolean);
  const [first = '', second, third] = segments.map((segment) => segment.toLowerCase());
  if (first === 'app' && second === 'r' && third) return `/app/r/${segments[2]}`;
  if (first === 'app' && second && second !== 'r') return `/app/r/${segments[1]}`;
  if (['r', 'repo', 'repos', 'workspace'].includes(first)) return second ? `/app/r/${segments[1]}` : '/app';
  if (['guide', 'guides', 'documentation', 'help'].includes(first)) return '/docs';
  if (['plans', 'billing', 'price', 'prices'].includes(first)) return '/pricing';
  if (['settings', 'profile', 'me'].includes(first)) return '/account';
  const close = KNOWN_SECTIONS
    .map((section) => [section, editDistance(first, section)])
    .filter(([, distance]) => distance > 0 && distance <= 2)
    .sort((a, b) => a[1] - b[1])[0];
  return close ? `/${close[0]}` : null;
};

const NotFoundPage = () => {
  const { pathname } = useLocation();
  const signedIn = useStore((state) => Boolean(state.user));
  const suggestion = suggestPath(pathname);
  return (
    <main id="main" className="content-shell flex min-h-[calc(100dvh-3.5rem)] flex-col items-center justify-center py-16 text-center">
      <span className="flex h-12 w-12 items-center justify-center rounded-2xl bg-accent-soft text-accent-strong">
        <Compass className="h-6 w-6" aria-hidden="true" />
      </span>
      <p className="eyebrow mt-6">Error 404</p>
      <h1 className="display-title mt-3 text-[2.25rem] text-ink sm:text-5xl">This page does not exist</h1>
      <p className="mt-4 max-w-md text-[15px] leading-relaxed text-ink-2">
        Nothing lives at <code className="break-all rounded-md border border-line bg-surface-2 px-1.5 py-0.5 font-mono text-[0.85em] text-ink">{pathname}</code>.
        {' '}The link may be mistyped, or the page may have moved.
      </p>
      {suggestion && (
        <p className="mt-5 text-[15px] text-ink-2">
          Did you mean{' '}
          <Link to={suggestion} className="font-medium text-accent-strong underline decoration-accent/40 underline-offset-4 hover:decoration-accent">{suggestion}</Link>?
        </p>
      )}
      <div className="mt-8 flex w-full flex-col justify-center gap-3 sm:w-auto sm:flex-row">
        <Button to="/" variant="primary" size="lg">Back to home</Button>
        {signedIn
          ? <Button to="/app" variant="secondary" size="lg" iconRight={ArrowRight}>Open workspace</Button>
          : <Button to="/docs" variant="secondary" size="lg" iconRight={ArrowRight}>Read the docs</Button>}
      </div>
      <nav aria-label="Other pages" className="mt-10 flex flex-wrap justify-center gap-x-1 gap-y-2 text-sm">
        {[['Platform', '/platform'], ['Pricing', '/pricing'], ['Docs', '/docs']].map(([label, to]) => (
          <Link key={to} to={to} className="rounded-lg px-3 py-2 text-ink-3 transition-colors hover:bg-surface-2 hover:text-ink">{label}</Link>
        ))}
      </nav>
    </main>
  );
};

/* ---------- Layouts and titles ---------- */

const MarketingLayout = ({ children }) => (
  <div className="min-h-screen bg-canvas text-ink">
    <SiteHeader />
    <RouteBoundary fallback={<PageSkeleton />}>{children}</RouteBoundary>
  </div>
);

const WorkspaceRoute = ({ children }) => (
  <RouteBoundary fallback={<WorkspaceSkeleton />} fullscreen>{children}</RouteBoundary>
);

const ROUTE_TITLES = {
  '/': '',
  '/platform': 'Platform',
  '/pricing': 'Pricing',
  '/docs': 'Docs',
  '/account': 'Account',
  '/app': 'Workspace',
  '/dashboard': 'Workspace',
  '/github/callback': 'Connecting GitHub',
};

const routeTitle = (pathname) => {
  const path = pathname.length > 1 ? pathname.replace(/\/+$/, '') : pathname;
  const repo = matchPath('/app/r/:repoName', path);
  if (repo) {
    try {
      return decodeURIComponent(repo.params.repoName);
    } catch {
      return repo.params.repoName;
    }
  }
  return path in ROUTE_TITLES ? ROUTE_TITLES[path] : 'Page not found';
};

function App() {
  const user = useStore((state) => state.user);
  const setUser = useStore((state) => state.setUser);
  const fetchRepos = useStore((state) => state.fetchRepos);
  const pollStatuses = useStore((state) => state.pollStatuses);
  const indexing = useStore((state) => state.repos.some((repo) => repo.is_indexing));
  const [authLoading, setAuthLoading] = useState(true);
  const navigate = useNavigate();
  const location = useLocation();

  // Per-route tab titles. A layout effect, so a page's own useDocumentTitle
  // (a passive effect, which runs later) can refine it.
  const title = routeTitle(location.pathname);
  useLayoutEffect(() => {
    document.title = formatTitle(title);
  }, [title]);

  // The workspace is a fixed-height shell that manages its own scrolling
  // and focus; everything else gets top-of-page + heading focus on arrival.
  useScrollRestoration({ ignore: (next) => isWorkspacePath(next.pathname) });
  useRouteFocus({
    shouldFocus: (next) => !isWorkspacePath(next.pathname),
    shouldAnnounce: (_next, from) => !isWorkspacePath(from),
  });

  useEffect(() => {
    applyTheme();
  }, []);

  useEffect(() => {
    let mounted = true;
    supabase.auth.getSession().then(({ data: { session } }) => {
      if (!mounted) return;
      setUser(session?.user ?? null);
      setAuthLoading(false);
    });
    const { data: { subscription } } = supabase.auth.onAuthStateChange((_event, session) => {
      if (!mounted) return;
      setUser(session?.user ?? null);
      setAuthLoading(false);
    });
    return () => {
      mounted = false;
      subscription.unsubscribe();
    };
  }, [setUser]);

  useEffect(() => {
    if (!user) return;
    fetchRepos();
    try {
      const destination = window.sessionStorage.getItem(POST_LOGIN_KEY);
      if (destination) {
        window.sessionStorage.removeItem(POST_LOGIN_KEY);
        if (destination.startsWith('/') && !destination.startsWith('//')) navigate(destination, { replace: true });
      }
    } catch {
      // Ignore storage failures.
    }
  }, [user, fetchRepos, navigate]);

  // Poll only while something is indexing; quick enough to feel live,
  // paused while the tab is hidden.
  useEffect(() => {
    if (!user || !indexing) return undefined;
    const tick = () => {
      if (document.visibilityState !== 'hidden') pollStatuses();
    };
    const interval = window.setInterval(tick, 3000);
    document.addEventListener('visibilitychange', tick);
    return () => {
      window.clearInterval(interval);
      document.removeEventListener('visibilitychange', tick);
    };
  }, [pollStatuses, user, indexing]);

  if (authLoading) {
    return (
      <>
        {isWorkspacePath(location.pathname) ? <WorkspaceSkeleton /> : <PageLoader fullscreen />}
        <ServerStatus />
        <Toaster />
      </>
    );
  }

  return (
    <>
      <Routes>
        <Route path="/" element={<MarketingLayout><LandingPage /></MarketingLayout>} />
        <Route path="/platform" element={<MarketingLayout><PlatformPage /></MarketingLayout>} />
        <Route path="/pricing" element={<MarketingLayout><PricingPage /></MarketingLayout>} />
        <Route path="/docs" element={<MarketingLayout><DocsPage /></MarketingLayout>} />
        <Route path="/account" element={<RequireAuth><MarketingLayout><AccountPage /></MarketingLayout></RequireAuth>} />
        <Route path="/app" element={<RequireAuth><WorkspaceRoute><DashboardPage /></WorkspaceRoute></RequireAuth>} />
        <Route path="/app/r/:repoName" element={<RequireAuth><WorkspaceRoute><DashboardPage /></WorkspaceRoute></RequireAuth>} />
        <Route path="/github/callback" element={<RouteBoundary fallback={<PageLoader fullscreen label="Connecting GitHub" />} fullscreen><GithubCallbackPage /></RouteBoundary>} />
        <Route path="/dashboard" element={<Navigate to="/app" replace />} />
        <Route path="*" element={<MarketingLayout><NotFoundPage /></MarketingLayout>} />
      </Routes>
      <ServerStatus />
      <Toaster />
    </>
  );
}

export default App;
