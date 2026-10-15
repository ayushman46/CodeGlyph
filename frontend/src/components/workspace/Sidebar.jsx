import { AlertTriangle, FolderGit2, RefreshCw } from 'lucide-react';
import { useState } from 'react';
import { NavLink } from 'react-router-dom';
import { formatCount } from '../../lib/format';
import { overallProgress, phaseLabel, statusSentence } from '../../lib/phase';
import useStore from '../../store/useStore';
import ProgressBar from '../ui/ProgressBar';
import Spinner from '../ui/Spinner';
import RepoInput from './RepoInput';

const dotClass = (repo) => {
  if (repo.status === 'failed') return 'bg-danger';
  if (repo.status === 'cancelled') return 'bg-ink-3';
  if (repo.is_indexing) return 'bg-warning animate-pulse';
  return 'bg-success';
};

const Sidebar = ({ onNavigate }) => {
  const repos = useStore((state) => state.repos);
  const reposLoaded = useStore((state) => state.reposLoaded);
  const reposError = useStore((state) => state.reposError);
  const fetchRepos = useStore((state) => state.fetchRepos);
  const usage = useStore((state) => state.accountUsage);
  const [retrying, setRetrying] = useState(false);
  // With no repositories the main panel is the place to add the first one;
  // a second identical field here would only compete with it.
  const firstRun = reposLoaded && !reposError && repos.length === 0;

  const retry = async () => {
    setRetrying(true);
    try {
      await fetchRepos();
    } finally {
      setRetrying(false);
    }
  };

  return (
    <div className="flex h-full min-h-0 flex-col">
      {!firstRun && (
        <div className="p-3">
          <RepoInput size="sm" showExamples={false} />
        </div>
      )}
      <div className={`flex items-center justify-between px-4 pb-1.5 ${firstRun ? 'pt-4' : 'pt-1'}`}>
        <h2 className="text-[11px] font-semibold uppercase tracking-wider text-ink-3">Repositories</h2>
        {reposLoaded && repos.length > 0 && <span className="font-mono text-[11px] text-ink-3" aria-label={`${repos.length} repositories`}>{repos.length}</span>}
      </div>
      <nav className="scroll-thin min-h-0 flex-1 space-y-0.5 overflow-y-auto px-2 pb-3" aria-label="Repositories" aria-busy={!reposLoaded || undefined}>
        {!reposLoaded && Array.from({ length: 4 }, (_, index) => <div key={index} className="skeleton mx-1 my-2 h-11" aria-hidden="true" />)}
        {reposError && (
          <div className="m-2 rounded-xl border border-danger/30 bg-danger-soft p-3 text-[12px] leading-relaxed text-danger">
            <p className="flex items-start gap-1.5"><AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" /> <span>{reposError}</span></p>
            <button type="button" onClick={retry} disabled={retrying} className="mt-2 inline-flex items-center gap-1.5 font-semibold underline underline-offset-2 disabled:opacity-60">
              {retrying ? <Spinner className="h-3 w-3" /> : <RefreshCw className="h-3 w-3" aria-hidden="true" />} Retry
            </button>
          </div>
        )}
        {firstRun && (
          <div className="mx-2 mt-1 rounded-xl border border-dashed border-line p-4 text-center">
            <FolderGit2 className="mx-auto h-5 w-5 text-ink-3" aria-hidden="true" />
            <p className="mt-2 text-[12.5px] font-medium text-ink-2">No repositories yet</p>
            <p className="mt-1 text-[12px] leading-relaxed text-ink-3">Repositories you index appear here, with their progress.</p>
          </div>
        )}
        {repos.map((repo) => (
          <NavLink
            key={repo.id}
            to={`/app/r/${encodeURIComponent(repo.repo_name)}`}
            onClick={onNavigate}
            className={({ isActive }) => `block rounded-xl px-2.5 py-2 transition-colors ${isActive ? 'bg-surface shadow-[var(--shadow-soft)] ring-1 ring-line' : 'hover:bg-surface-2'}`}
          >
            <div className="flex items-center gap-2">
              <span className={`h-2 w-2 shrink-0 rounded-full ${dotClass(repo)}`} aria-hidden="true" />
              <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-ink">{repo.repo_name}</span>
              <span className="sr-only">, {statusSentence(repo)}</span>
              {repo.queryable && (
                <span className="shrink-0 font-mono text-[10.5px] text-ink-3" title={`${formatCount(repo.indexed_files || 0)} indexed files`} aria-hidden="true">
                  {formatCount(repo.indexed_files || 0)}
                </span>
              )}
            </div>
            <p className={`mt-0.5 truncate pl-4 text-[11.5px] ${repo.status === 'failed' ? 'text-danger' : 'text-ink-3'}`} aria-hidden="true">
              {repo.is_indexing || repo.status !== 'ready' ? phaseLabel(repo) : repo.full_name}
            </p>
            {repo.is_indexing && (
              <div className="mt-1.5 pl-4" aria-hidden="true"><ProgressBar value={overallProgress(repo)} className="!h-1" /></div>
            )}
          </NavLink>
        ))}
      </nav>
      {usage && (
        <NavLink to="/account" onClick={onNavigate} className="mx-3 mb-3 block rounded-xl border border-line bg-surface px-3 py-2.5 transition-colors hover:border-line-strong">
          <div className="flex items-center justify-between text-[11.5px]">
            <span className="text-ink-3">Indexed source</span>
            <span className="font-medium text-ink-2">{usage.used_label} / {usage.quota_label}</span>
          </div>
          <ProgressBar value={(usage.used_bytes / Math.max(1, usage.quota_bytes)) * 100} tone={usage.used_bytes / Math.max(1, usage.quota_bytes) > 0.9 ? 'danger' : 'accent'} className="mt-2 !h-1" label="Indexed source quota" />
        </NavLink>
      )}
    </div>
  );
};

export default Sidebar;
