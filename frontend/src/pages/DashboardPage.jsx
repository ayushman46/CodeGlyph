import { AlertTriangle, ArrowRight, FolderGit2, RefreshCw, SearchX, X } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom';
import SiteHeader from '../components/SiteHeader';
import CommandPalette from '../components/workspace/CommandPalette';
import PullRequestModal from '../components/workspace/PullRequestModal';
import RepoDialogs from '../components/workspace/RepoDialogs';
import RepoInput from '../components/workspace/RepoInput';
import RepoWorkspace from '../components/workspace/RepoWorkspace';
import Sidebar from '../components/workspace/Sidebar';
import Badge from '../components/ui/Badge';
import Button from '../components/ui/Button';
import Kbd from '../components/ui/Kbd';
import ProgressBar from '../components/ui/ProgressBar';
import useFocusTrap from '../hooks/useFocusTrap';
import useMediaQuery from '../hooks/useMediaQuery';
import { formatCount, relativeTime } from '../lib/format';
import { overallProgress, phaseLabel, statusTone } from '../lib/phase';
import useStore from '../store/useStore';

const repoPath = (name) => `/app/r/${encodeURIComponent(name)}`;

const STEPS = [
  { title: 'Paste a public repository', text: 'A GitHub URL, owner/repo, or a link to a branch or commit. The most important files are indexed first.' },
  { title: 'Ask once keyword search is ready', text: 'Questions unlock as soon as keyword search is built; the semantic index keeps improving answers in the background.' },
  { title: 'Get cited answers and pull requests', text: 'Every answer cites file:line you can open. Ask for a change to get a reviewable diff, then open a PR.' },
];

const FirstRun = ({ prefill, autoFocus }) => (
  <div className="relative">
    <div className="bg-grid pointer-events-none absolute inset-x-0 top-0 h-80" aria-hidden="true" />
    <div className="accent-glow pointer-events-none absolute inset-x-0 top-0 h-80" aria-hidden="true" />
    <div className="relative mx-auto max-w-2xl px-4 pb-16 pt-10 sm:px-5 sm:pt-20">
      <p className="eyebrow text-center">Get started</p>
      <h1 className="display-title mt-3 text-center text-[2.1rem] text-ink sm:text-5xl">Paste a repo. Get cited answers.</h1>
      <p className="mx-auto mt-4 max-w-lg text-center text-[15px] leading-relaxed text-ink-3">
        Index any public GitHub repository. Keyword search is ready first, so you can start asking while the semantic index finishes.
      </p>
      <div className="mt-8"><RepoInput autoFocus={autoFocus} initialValue={prefill} /></div>
      <ol className="mt-10 grid gap-3 sm:mt-12">
        {STEPS.map((step, index) => (
          <li key={step.title} className="flex gap-3.5 rounded-2xl border border-line bg-surface p-4">
            <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-accent-soft font-mono text-[12px] font-semibold text-accent-strong" aria-hidden="true">{index + 1}</span>
            <span className="min-w-0">
              <span className="block text-[13.5px] font-semibold text-ink">{step.title}</span>
              <span className="mt-1 block text-[12.5px] leading-relaxed text-ink-3">{step.text}</span>
            </span>
          </li>
        ))}
      </ol>
    </div>
  </div>
);

const RepoCard = ({ repo }) => (
  <li>
    <Link to={repoPath(repo.repo_name)} className="group flex h-full flex-col rounded-2xl border border-line bg-surface p-4 transition-colors hover:border-line-strong hover:bg-surface-2/50">
      <span className="flex items-start justify-between gap-2">
        <span className="min-w-0">
          <span className="block truncate text-[14px] font-semibold text-ink">{repo.repo_name}</span>
          <span className="mt-0.5 block truncate text-[12px] text-ink-3">{repo.full_name}</span>
        </span>
        <ArrowRight className="mt-0.5 h-4 w-4 shrink-0 text-ink-3 transition-transform group-hover:translate-x-0.5" aria-hidden="true" />
      </span>
      <span className="mt-3 flex flex-wrap items-center gap-2 text-[12px] text-ink-3">
        <Badge tone={statusTone(repo)} dot>{phaseLabel(repo)}</Badge>
        {repo.queryable && <span>{formatCount(repo.indexed_files)} files</span>}
        {!repo.is_indexing && repo.indexed_at && <span>· {relativeTime(repo.indexed_at)}</span>}
      </span>
      {repo.is_indexing && <ProgressBar value={overallProgress(repo)} className="mt-3 !h-1" label={`${repo.repo_name} indexing progress`} />}
    </Link>
  </li>
);

const Returning = ({ repos, prefill, autoFocus }) => (
  <div className="mx-auto max-w-3xl px-4 pb-16 pt-8 sm:px-6 sm:pt-14">
    <p className="eyebrow">Workspace</p>
    <h1 className="mt-2 text-2xl font-semibold tracking-tight text-ink sm:text-3xl">Your repositories</h1>
    <p className="mt-2 text-sm leading-relaxed text-ink-3">Open one to ask questions and browse its code, or index another.</p>
    <div className="mt-6"><RepoInput autoFocus={autoFocus || Boolean(prefill)} initialValue={prefill} showExamples={false} /></div>
    <ul className="mt-8 grid gap-3 sm:grid-cols-2" aria-label="Your repositories">
      {repos.map((repo) => <RepoCard key={repo.id} repo={repo} />)}
    </ul>
    <p className="mt-6 hidden items-center gap-1.5 text-[12px] text-ink-3 lg:flex">
      Tip: press <Kbd>⌘</Kbd><Kbd>K</Kbd> to jump to any repository or action.
    </p>
  </div>
);

const WorkspaceHome = ({ repos }) => {
  const location = useLocation();
  const isDesktop = useMediaQuery('(min-width: 1024px)');
  const prefill = location.state?.prefill || '';
  return (
    <div className="scroll-thin h-full overflow-y-auto">
      {repos.length
        ? <Returning repos={repos} prefill={prefill} autoFocus={false} />
        // The keyboard would cover half the screen on phones, so only desktops autofocus.
        : <FirstRun prefill={prefill} autoFocus={isDesktop || Boolean(prefill)} />}
    </div>
  );
};

const ReposError = ({ message, onRetry, retrying }) => (
  <div className="flex h-full items-center justify-center p-6">
    <div className="w-full max-w-md rounded-2xl border border-danger/30 bg-danger-soft p-5" role="alert">
      <div className="flex items-start gap-2.5 text-danger">
        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden="true" />
        <div>
          <p className="text-sm font-semibold">Your repositories could not be loaded</p>
          <p className="mt-1 text-[13px] leading-relaxed">{message}</p>
          <p className="mt-1 text-[13px] leading-relaxed">Your indexes are safe; this is a connection problem.</p>
        </div>
      </div>
      <Button className="mt-4" size="sm" variant="secondary" icon={RefreshCw} loading={retrying} onClick={onRetry}>Retry</Button>
    </div>
  </div>
);

const NotFound = ({ name }) => (
  <div className="flex h-full items-center justify-center p-6">
    <div className="w-full max-w-md text-center">
      <div className="mx-auto flex h-11 w-11 items-center justify-center rounded-xl bg-surface-2 text-ink-3"><SearchX className="h-5 w-5" aria-hidden="true" /></div>
      <h1 className="mt-4 text-base font-semibold text-ink">Repository not found</h1>
      <p className="mt-1.5 text-sm leading-relaxed text-ink-3">
        There is no repository named <span className="font-mono text-ink-2">{name}</span> in your workspace. It may have been renamed or deleted.
      </p>
      <Button className="mt-5" variant="primary" size="sm" icon={FolderGit2} to="/app">Go to your repositories</Button>
    </div>
  </div>
);

const LoadingWorkspace = ({ withRepo }) => (
  <div className="h-full" aria-busy="true">
    <span className="sr-only" role="status">Loading your workspace</span>
    {withRepo ? (
      <div aria-hidden="true">
        <div className="flex items-center justify-between border-b border-line px-4 py-3 sm:px-6">
          <div className="space-y-2"><div className="skeleton h-4 w-32" /><div className="skeleton h-3 w-44" /></div>
          <div className="skeleton h-8 w-36 rounded-xl" />
        </div>
        <div className="mx-auto max-w-3xl space-y-3 px-4 py-8 sm:px-6">
          <div className="skeleton h-3 w-24" /><div className="skeleton h-7 w-3/4" /><div className="skeleton h-4 w-full" />
          <div className="grid gap-2 pt-4 sm:grid-cols-2">{Array.from({ length: 4 }, (_, index) => <div key={index} className="skeleton h-11 rounded-xl" />)}</div>
        </div>
      </div>
    ) : (
      <div className="mx-auto max-w-3xl space-y-3 px-4 pt-14 sm:px-6" aria-hidden="true">
        <div className="skeleton h-3 w-24" /><div className="skeleton h-8 w-2/3" /><div className="skeleton h-14 w-full rounded-2xl" />
      </div>
    )}
  </div>
);

const DashboardPage = () => {
  const { repoName: encodedName } = useParams();
  const repoName = encodedName ? decodeURIComponent(encodedName) : null;
  const navigate = useNavigate();
  const repos = useStore((state) => state.repos);
  const reposLoaded = useStore((state) => state.reposLoaded);
  const reposError = useStore((state) => state.reposError);
  const fetchRepos = useStore((state) => state.fetchRepos);
  const [retrying, setRetrying] = useState(false);
  const selectRepo = useStore((state) => state.selectRepo);
  const sourcesOpen = useStore((state) => state.sources.open);
  const closeSources = useStore((state) => state.closeSources);
  const review = useStore((state) => state.review);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [dialog, setDialog] = useState(null);
  const drawerRef = useRef(null);
  const isLarge = useMediaQuery('(min-width: 1024px)');
  const location = useLocation();
  const exact = repos.find((item) => item.repo_name === repoName) || null;
  // A rename (here or in another tab) keeps the open repository: follow it
  // to its new name instead of reporting it missing.
  const lastShown = useRef(null);
  const renamed = !exact && repoName && lastShown.current?.name === repoName
    ? repos.find((item) => item.id === lastShown.current.id) || null
    : null;
  const repo = exact || renamed;
  const missing = Boolean(repoName && reposLoaded && !reposError && !repo);
  useEffect(() => {
    if (exact) lastShown.current = { id: exact.id, name: exact.repo_name };
  }, [exact?.id, exact?.repo_name]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (renamed) navigate({ pathname: repoPath(renamed.repo_name), search: location.search }, { replace: true, state: location.state });
  }, [renamed?.repo_name]); // eslint-disable-line react-hooks/exhaustive-deps

  // The URL is the source of truth for the selected repository.
  useEffect(() => {
    // A failed load is not "no such repository": keep the deep link until
    // the list loads, then select it.
    if (!reposLoaded || reposError) return;
    void selectRepo(repo ? repo.repo_name : null);
  }, [repoName, repo?.repo_name, reposLoaded, reposError, selectRepo]); // eslint-disable-line react-hooks/exhaustive-deps

  const retryRepos = async () => {
    setRetrying(true);
    try {
      await fetchRepos();
    } finally {
      setRetrying(false);
    }
  };

  // A repository becoming queryable (indexing reached keyword search) loads
  // its conversation once.
  useEffect(() => {
    if (repo?.queryable) void selectRepo(repo.repo_name);
  }, [repo?.queryable]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (isLarge) setDrawerOpen(false);
  }, [isLarge]);

  const onKeyDown = useCallback((event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      setPaletteOpen((value) => !value);
    } else if (event.key === 'Escape' && !paletteOpen && !dialog && !review) {
      if (sourcesOpen) closeSources();
      else if (drawerOpen) setDrawerOpen(false);
    }
  }, [paletteOpen, dialog, review, sourcesOpen, closeSources, drawerOpen]);

  useEffect(() => {
    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [onKeyDown]);

  useFocusTrap(drawerRef, drawerOpen, { onEscape: () => setDrawerOpen(false) });

  const requestDialog = useCallback((type, target) => setDialog({ type, repo: target }), []);
  const addRepository = useCallback(() => {
    navigate('/app');
    window.setTimeout(() => document.getElementById('repo-input-lg')?.focus(), 80);
  }, [navigate]);
  const showTab = useCallback((tab) => {
    if (!repo) return;
    navigate({ pathname: repoPath(repo.repo_name), search: tab === 'overview' ? '?tab=overview' : '' }, { replace: true });
  }, [navigate, repo]);

  let main;
  if (reposError && !repo && !repos.length) main = <ReposError message={reposError} onRetry={retryRepos} retrying={retrying} />;
  else if (!reposLoaded) main = <LoadingWorkspace withRepo={Boolean(repoName)} />;
  else if (missing) main = <NotFound name={repoName} />;
  else if (repo) main = <RepoWorkspace repo={repo} onRequestDialog={requestDialog} />;
  else main = <WorkspaceHome repos={repos} />;

  return (
    <div className="flex h-dvh flex-col overflow-hidden bg-canvas text-ink">
      <SiteHeader variant="app" onToggleSidebar={() => setDrawerOpen(true)} onOpenPalette={() => setPaletteOpen(true)} />
      <div className="flex min-h-0 flex-1">
        <aside className="hidden w-72 shrink-0 border-r border-line bg-surface-2/40 lg:block" aria-label="Repositories sidebar">
          <Sidebar />
        </aside>
        {drawerOpen && (
          <div className="fixed inset-0 z-[60] lg:hidden" role="dialog" aria-modal="true" aria-label="Repositories">
            <button type="button" tabIndex={-1} className="absolute inset-0 bg-black/40 animate-fade-in" onClick={() => setDrawerOpen(false)} aria-label="Close repositories" />
            <div ref={drawerRef} tabIndex={-1} className="relative flex h-full w-[min(20rem,88vw)] flex-col border-r border-line bg-canvas shadow-[var(--shadow-lift)] outline-none animate-rise">
              <div className="flex h-14 shrink-0 items-center justify-between border-b border-line px-4">
                <p className="text-sm font-semibold text-ink">Switch repository</p>
                <button type="button" onClick={() => setDrawerOpen(false)} className="-mr-1.5 flex h-9 w-9 items-center justify-center rounded-lg text-ink-3 hover:bg-surface-2 hover:text-ink" aria-label="Close"><X className="h-4 w-4" /></button>
              </div>
              <div className="min-h-0 flex-1"><Sidebar onNavigate={() => setDrawerOpen(false)} /></div>
            </div>
          </div>
        )}
        <main className="min-w-0 flex-1">{main}</main>
      </div>
      <RepoDialogs dialog={dialog} onClose={() => setDialog(null)} />
      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        onAddRepository={addRepository}
        currentRepo={repo}
        onRepoAction={requestDialog}
        onShowTab={showTab}
      />
      <PullRequestModal />
    </div>
  );
};

export default DashboardPage;
