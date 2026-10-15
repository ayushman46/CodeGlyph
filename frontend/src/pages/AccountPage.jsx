import {
  AlertCircle, AlertTriangle, ArrowRight, CalendarDays, CreditCard, Database, ExternalLink, FolderGit2, Github,
  LogOut, Mail, Plus, RefreshCw, ShieldCheck, Unplug,
} from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { disconnectGithub, errorMessage, getGithubLoginUrl } from '../api/client';
import { Avatar } from '../components/SiteHeader';
import Badge from '../components/ui/Badge';
import Button from '../components/ui/Button';
import ProgressBar from '../components/ui/ProgressBar';
import Spinner from '../components/ui/Spinner';
import { formatBytes, relativeTime } from '../lib/format';
import { phaseLabel, statusTone } from '../lib/phase';
import useStore from '../store/useStore';

const GITHUB_POPUP = 'codebase-intel-github';
const GITHUB_CONNECT_TIMEOUT_MS = 5 * 60 * 1000;
const REPO_LIST_LIMIT = 6;

const Card = ({ className = '', labelledBy, children }) => (
  <section aria-labelledby={labelledBy} className={`min-w-0 rounded-3xl border border-line bg-surface p-5 shadow-[var(--shadow-soft)] sm:p-7 ${className}`}>
    {children}
  </section>
);

// A small aside (badge, icon button) stays beside the title on phones; a
// full-width one (wrapAside) drops below it.
const CardHeader = ({ icon: Icon, id, title, badge, description, aside, wrapAside = false }) => (
  <div className={`flex items-start justify-between gap-3 ${wrapAside ? 'flex-wrap' : ''}`}>
    <div className="flex min-w-0 items-start gap-3">
      <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border border-line bg-surface-2 text-ink-2">
        <Icon className="h-4 w-4" aria-hidden="true" />
      </span>
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          <h2 id={id} className="text-[15px] font-semibold text-ink">{title}</h2>
          {badge}
        </div>
        {description && <p className="mt-0.5 text-[13px] leading-relaxed text-ink-2">{description}</p>}
      </div>
    </div>
    {aside && <div className={wrapAside ? 'w-full sm:w-auto' : 'shrink-0'}>{aside}</div>}
  </div>
);

const InlineError = ({ children, onRetry, retryLabel = 'Try again', retrying = false }) => (
  <div role="alert" className="flex flex-wrap items-start gap-3 rounded-2xl border border-danger/25 bg-danger-soft p-4">
    <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-danger" aria-hidden="true" />
    <p className="min-w-0 flex-1 text-sm leading-relaxed text-danger">{children}</p>
    {onRetry && <Button size="xs" variant="secondary" onClick={onRetry} loading={retrying}>{retryLabel}</Button>}
  </div>
);

/** A short, actionable note under the storage meter. */
const Notice = ({ tone = 'warning', title, children, action }) => {
  const styles = tone === 'danger'
    ? 'border-danger/25 bg-danger-soft text-danger'
    : 'border-warning/30 bg-warning-soft text-warning';
  return (
    <div className={`mt-5 flex flex-col gap-3 rounded-2xl border p-4 sm:flex-row sm:items-start ${styles}`}>
      <AlertTriangle className="mt-0.5 hidden h-4 w-4 shrink-0 sm:block" aria-hidden="true" />
      <div className="min-w-0 flex-1 text-[13px] leading-relaxed">
        <p className="font-semibold">{title}</p>
        <p className="mt-0.5 text-ink-2">{children}</p>
      </div>
      {action}
    </div>
  );
};

const repoSize = (repo) => {
  const bytes = Number(repo?.selection?.selected_bytes) || 0;
  return !repo.is_indexing && repo.status !== 'failed' && repo.status !== 'cancelled' ? bytes : 0;
};

const AccountPage = () => {
  const user = useStore((state) => state.user);
  const repos = useStore((state) => state.repos);
  const reposLoaded = useStore((state) => state.reposLoaded);
  const reposError = useStore((state) => state.reposError);
  const fetchRepos = useStore((state) => state.fetchRepos);
  const signOut = useStore((state) => state.signOut);
  const usage = useStore((state) => state.accountUsage);
  const usageLoading = useStore((state) => state.accountUsageLoading);
  const usageError = useStore((state) => state.accountUsageError);
  const fetchAccountUsage = useStore((state) => state.fetchAccountUsage);
  const github = useStore((state) => state.github);
  const checkGithub = useStore((state) => state.checkGithub);
  const setGithub = useStore((state) => state.setGithub);
  const navigate = useNavigate();

  const [githubCheckFailed, setGithubCheckFailed] = useState(false);
  const [githubRetrying, setGithubRetrying] = useState(false);
  const [githubError, setGithubError] = useState('');
  const [githubNotice, setGithubNotice] = useState('');
  const [confirmDisconnect, setConfirmDisconnect] = useState(false);
  const [disconnecting, setDisconnecting] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [reposRetrying, setReposRetrying] = useState(false);
  const [signingOut, setSigningOut] = useState(false);
  const mountedRef = useRef(true);
  const popupRef = useRef(null);
  const pollRef = useRef(0);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      window.clearInterval(pollRef.current);
    };
  }, []);

  const fullName = user?.user_metadata?.full_name || user?.email?.split('@')[0] || 'Workspace member';
  const createdAt = user?.created_at ? new Date(user.created_at).toLocaleDateString(undefined, { month: 'long', year: 'numeric' }) : 'Recently';

  // Load usage now, then keep it fresh while the page is open (indexing in
  // another tab changes it) and whenever the window regains focus.
  useEffect(() => {
    if (!user?.id) return undefined;
    void fetchAccountUsage();
    const refresh = () => {
      if (document.visibilityState !== 'hidden') void fetchAccountUsage({ silent: true });
    };
    const interval = window.setInterval(refresh, 10000);
    window.addEventListener('focus', refresh);
    return () => {
      window.clearInterval(interval);
      window.removeEventListener('focus', refresh);
    };
  }, [user?.id, fetchAccountUsage]);

  useEffect(() => {
    if (!user?.id) return undefined;
    let active = true;
    // checkGithub resolves to null when the status request itself failed.
    checkGithub().then((result) => {
      if (active) setGithubCheckFailed(result === null);
    });
    return () => {
      active = false;
    };
  }, [user?.id, checkGithub]);

  // ------------------------------------------------------------ GitHub

  const finishConnecting = (status, problem = '') => {
    window.clearInterval(pollRef.current);
    pollRef.current = 0;
    try {
      if (popupRef.current && !popupRef.current.closed && status?.connected) popupRef.current.close();
    } catch {
      // A cross-origin popup may refuse; it closes itself after success.
    }
    popupRef.current = null;
    if (!mountedRef.current) return;
    setConnecting(false);
    if (status?.connected) {
      setGithub(status);
      setGithubCheckFailed(false);
      setGithubNotice(status.username ? `Connected as @${status.username}.` : 'GitHub connected.');
    } else if (problem) {
      setGithubError(problem);
    }
  };

  // The OAuth popup reports its result with postMessage (see
  // GithubCallbackPage); polling below covers a popup that lost its opener.
  useEffect(() => {
    const onMessage = (event) => {
      if (event.origin !== window.location.origin || !popupRef.current || event.source !== popupRef.current) return;
      if (event.data?.type === 'GITHUB_AUTH_SUCCESS') {
        void checkGithub().then((status) => finishConnecting(status || { connected: true, username: event.data.github_username || '' }));
      } else if (event.data?.type === 'GITHUB_AUTH_ERROR') {
        finishConnecting(null, event.data.error || 'GitHub authorization failed.');
      }
    };
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
    // finishConnecting only touches refs and stable setters.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [checkGithub]);

  const connectGithub = async () => {
    setGithubError('');
    setGithubNotice('');
    setConnecting(true);
    // Open the window inside the click so pop-up blockers allow it, then
    // point it at GitHub once the server has issued the authorization URL.
    const width = 600;
    const height = 720;
    const left = window.screenX + Math.max(0, (window.outerWidth - width) / 2);
    const top = window.screenY + Math.max(0, (window.outerHeight - height) / 2);
    const popup = window.open('', GITHUB_POPUP, `width=${width},height=${height},left=${left},top=${top}`);
    if (!popup) {
      setConnecting(false);
      setGithubError('Your browser blocked the GitHub window. Allow pop-ups for this site, then try again.');
      return;
    }
    popupRef.current = popup;
    try {
      const response = await getGithubLoginUrl('/account');
      const url = response.data?.authorization_url;
      if (!url) throw new Error('GitHub authorization is unavailable right now.');
      popup.location.href = url;
    } catch (error) {
      try { popup.close(); } catch { /* already closed */ }
      popupRef.current = null;
      if (mountedRef.current) {
        setConnecting(false);
        setGithubError(errorMessage(error, error?.message || 'GitHub authorization could not be started.'));
      }
      return;
    }
    const startedAt = github.updatedAt;
    const deadline = Date.now() + GITHUB_CONNECT_TIMEOUT_MS;
    window.clearInterval(pollRef.current);
    pollRef.current = window.setInterval(async () => {
      const closed = !popupRef.current || popupRef.current.closed;
      const status = await checkGithub();
      if (status?.connected && status.updatedAt !== startedAt) {
        finishConnecting(status);
      } else if (closed) {
        finishConnecting(null, 'The GitHub window was closed before the connection finished. Nothing was connected.');
      } else if (Date.now() > deadline) {
        finishConnecting(null, 'GitHub did not respond in time. Close the GitHub window and try again.');
      }
    }, 2000);
  };

  const retryGithubCheck = async () => {
    setGithubError('');
    setGithubRetrying(true);
    const result = await checkGithub();
    if (mountedRef.current) {
      setGithubCheckFailed(result === null);
      setGithubRetrying(false);
    }
  };

  const handleDisconnect = async () => {
    setDisconnecting(true);
    setGithubError('');
    setGithubNotice('');
    try {
      await disconnectGithub();
      const result = await checkGithub();
      if (mountedRef.current) {
        setGithubCheckFailed(result === null);
        setConfirmDisconnect(false);
        setGithubNotice('GitHub disconnected. The stored token was deleted.');
      }
    } catch (error) {
      if (mountedRef.current) setGithubError(errorMessage(error, 'GitHub could not be disconnected. Please try again.'));
    } finally {
      if (mountedRef.current) setDisconnecting(false);
    }
  };

  const retryRepos = async () => {
    setReposRetrying(true);
    await fetchRepos();
    if (mountedRef.current) setReposRetrying(false);
  };

  const handleSignOut = async () => {
    setSigningOut(true);
    try {
      await signOut();
    } catch {
      // The local session is cleared even if the provider call fails.
    }
    navigate('/', { replace: true });
  };

  // ------------------------------------------------------------ usage

  const usedBytes = Number(usage?.used_bytes) || 0;
  const quotaBytes = Math.max(1, Number(usage?.quota_bytes) || 0);
  const rawPercent = (usedBytes / quotaBytes) * 100;
  const usagePercent = usage ? Math.min(100, usedBytes > 0 ? Math.max(1, Math.round(rawPercent)) : 0) : 0;
  const percentLabel = usedBytes > 0 && rawPercent < 1 ? '<1%' : `${Math.round(rawPercent)}%`;
  const overLimit = usage && usedBytes > quotaBytes;
  const full = usage && !overLimit && quotaBytes - usedBytes < 1000;
  const nearLimit = usage && !overLimit && !full && rawPercent >= 90;
  const isTeam = usage?.plan === 'team';
  const expired = usage?.status === 'expired';
  const planName = isTeam ? 'Team' : 'Explorer';
  const planCta = expired ? 'Renew Team' : 'Upgrade to Team';
  const makeRoom = isTeam
    ? 'Delete a repository you no longer need to make room.'
    : `Delete a repository you no longer need, or ${expired ? 'renew' : 'move to'} Team for 800 MB.`;

  const indexingCount = repos.filter((repo) => repo.is_indexing).length;
  const sortedRepos = [...repos].sort((a, b) => repoSize(b) - repoSize(a) || a.repo_name.localeCompare(b.repo_name));
  const shownRepos = sortedRepos.slice(0, REPO_LIST_LIMIT);

  return (
    <main className="relative pb-20">
      <div className="pointer-events-none absolute inset-x-0 top-0 h-72" aria-hidden="true">
        <div className="bg-grid absolute inset-0 opacity-60" />
      </div>

      <div className="narrow-shell relative pt-12 sm:pt-16">
        <header className="min-w-0">
          <p className="eyebrow">Account</p>
          <h1 className="display-title mt-3 text-4xl text-ink sm:text-5xl">Your account</h1>
          <p className="mt-3 max-w-xl text-base leading-relaxed text-ink-2">
            Storage used, your plan, the repositories taking up space, and your GitHub connection.
          </p>
        </header>

        <div className="mt-10 grid animate-rise gap-4 md:grid-cols-3">
          {/* Profile */}
          <Card labelledBy="profile-heading" className="md:col-span-3">
            <div className="flex flex-col gap-5 sm:flex-row sm:items-center">
              <div className="flex min-w-0 flex-1 items-center gap-4">
                <Avatar user={user} size="h-12 w-12 text-base! sm:h-16 sm:w-16 sm:text-lg!" />
                <div className="min-w-0 flex-1">
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                    <h2 id="profile-heading" className="section-title truncate text-xl text-ink sm:text-2xl">{fullName}</h2>
                    <Badge tone="neutral">Google account</Badge>
                  </div>
                  <dl className="mt-2 flex flex-col gap-1.5 text-sm text-ink-2 sm:flex-row sm:flex-wrap sm:gap-x-6">
                    <div className="flex min-w-0 items-center gap-2">
                      <dt><Mail className="h-4 w-4 text-ink-3" aria-hidden="true" /><span className="sr-only">Email</span></dt>
                      <dd className="truncate">{user?.email || 'No email on file'}</dd>
                    </div>
                    <div className="flex items-center gap-1.5">
                      <dt className="flex items-center gap-2">
                        <CalendarDays className="h-4 w-4 text-ink-3" aria-hidden="true" />
                        Member since
                      </dt>
                      <dd>{createdAt}</dd>
                    </div>
                  </dl>
                </div>
              </div>
              <Button variant="secondary" size="sm" icon={LogOut} loading={signingOut} onClick={handleSignOut} className="w-full sm:w-auto">
                Sign out
              </Button>
            </div>
          </Card>

          {/* Indexed storage */}
          <Card labelledBy="storage-heading" className="md:col-span-2">
            <CardHeader
              icon={Database}
              id="storage-heading"
              title="Indexed storage"
              description="Total indexed source across all of your repositories."
              aside={(
                <Button
                  variant="ghost"
                  size="icon-sm"
                  onClick={() => fetchAccountUsage()}
                  disabled={usageLoading}
                  aria-label="Refresh usage"
                  title="Refresh usage"
                >
                  {usageLoading ? <Spinner className="h-3.5 w-3.5" /> : <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />}
                </Button>
              )}
            />

            <div className="mt-6">
              {usage ? (
                <>
                  <p className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                    <span className="display-title text-4xl text-ink">{usage.used_label}</span>
                    <span className="text-sm text-ink-2">of {usage.quota_label} used</span>
                  </p>
                  <ProgressBar
                    value={usagePercent}
                    tone={overLimit || full || nearLimit ? 'danger' : 'accent'}
                    label={`Indexed storage used: ${percentLabel} of ${usage.quota_label}`}
                    className="mt-4"
                  />
                  <div className="mt-2.5 flex flex-wrap items-center justify-between gap-2 text-[13px]">
                    <span className={overLimit || full || nearLimit ? 'font-medium text-danger' : 'text-ink-2'}>
                      {overLimit ? `${formatBytes(usedBytes - quotaBytes)} over the limit` : `${usage.remaining_label} remaining`}
                    </span>
                    <span className="font-mono text-ink-2">{percentLabel}</span>
                  </div>

                  {overLimit && (
                    <Notice tone="danger" title={`Over the ${planName} limit`}>
                      Indexed repositories keep answering questions, but nothing new can be indexed until there is free
                      space. {makeRoom}
                    </Notice>
                  )}
                  {full && (
                    <Notice tone="danger" title="Storage is full">
                      New indexing stops with a message until there is free space. {makeRoom}
                    </Notice>
                  )}
                  {nearLimit && (
                    <Notice title={`Almost full: ${usage.remaining_label} left`}>
                      A repository added now is indexed only up to the space left, most important files first; the rest are
                      listed as over budget. {makeRoom}
                    </Notice>
                  )}

                  {usage.usage_estimated && (
                    <p className="mt-4 text-xs leading-relaxed text-ink-2">
                      Includes an older index measured from its stored code sections. Re-index that repository once to
                      record its exact size.
                    </p>
                  )}
                </>
              ) : usageError ? (
                <InlineError onRetry={() => fetchAccountUsage()} retrying={usageLoading}>{usageError}</InlineError>
              ) : (
                <div role="status" aria-label="Loading usage">
                  <div className="skeleton h-9 w-40" />
                  <div className="skeleton mt-4 h-2 w-full" />
                  <div className="skeleton mt-3 h-3.5 w-32" />
                </div>
              )}
            </div>
          </Card>

          {/* Plan */}
          <Card labelledBy="plan-heading" className="flex flex-col">
            <CardHeader icon={CreditCard} id="plan-heading" title="Plan" description="What your workspace can hold." />
            <div className="mt-6 flex flex-1 flex-col">
              {usage ? (
                <>
                  <div className="flex flex-wrap items-center gap-2">
                    <p className="display-title text-3xl text-ink">{planName}</p>
                    {isTeam ? <Badge tone="success" dot>Active</Badge> : expired ? <Badge tone="warning">Team ended</Badge> : <Badge tone="neutral">Free</Badge>}
                  </div>
                  <p className="mt-2 text-[13px] leading-relaxed text-ink-2">
                    {isTeam
                      ? 'Up to 800 MB for 30 days from your payment. After that, the 200 MB Explorer limit applies; nothing renews.'
                      : expired
                        ? 'Your 30 days of Team ended, so the 200 MB Explorer limit applies again.'
                        : 'Up to 200 MB of indexed source. Team raises it to 800 MB for 30 days with a one-time ₹300 payment.'}
                  </p>
                  <div className="mt-auto pt-5">
                    {isTeam ? (
                      <Button to="/pricing" variant="secondary" size="sm" iconRight={ArrowRight} className="w-full">
                        Plan details
                      </Button>
                    ) : (
                      <Button to="/pricing" variant="dark" size="sm" iconRight={ArrowRight} className="w-full">
                        {planCta}
                      </Button>
                    )}
                  </div>
                </>
              ) : usageError ? (
                <p className="text-[13px] leading-relaxed text-ink-2">
                  Your plan appears once usage loads. <Link to="/pricing" className="font-medium text-accent-strong hover:underline">Compare plans</Link>
                </p>
              ) : (
                <div role="status" aria-label="Loading plan">
                  <div className="skeleton h-8 w-28" />
                  <div className="skeleton mt-3 h-3.5 w-full" />
                  <div className="skeleton mt-2 h-3.5 w-2/3" />
                </div>
              )}
            </div>
          </Card>

          {/* Repositories */}
          <Card labelledBy="repos-heading" className="md:col-span-3">
            <CardHeader
              icon={FolderGit2}
              id="repos-heading"
              title="Repositories"
              description={reposLoaded && repos.length
                ? `${repos.length} in your workspace${indexingCount ? `, ${indexingCount} indexing now` : ''}. Largest first.`
                : 'What is using your storage.'}
              wrapAside
              aside={reposLoaded && repos.length > 0 ? (
                <Button to="/app" variant="secondary" size="sm" iconRight={ArrowRight} className="w-full sm:w-auto">
                  Open workspace
                </Button>
              ) : null}
            />
            <div className="mt-5">
              {reposError && !repos.length ? (
                <InlineError onRetry={retryRepos} retrying={reposRetrying}>{reposError}</InlineError>
              ) : !reposLoaded ? (
                <div role="status" aria-label="Loading repositories" className="space-y-3">
                  <div className="skeleton h-10 w-full" />
                  <div className="skeleton h-10 w-full" />
                </div>
              ) : repos.length === 0 ? (
                <div className="flex flex-col items-start gap-4 rounded-2xl border border-dashed border-line px-5 py-6 sm:flex-row sm:items-center sm:justify-between">
                  <div>
                    <p className="text-sm font-semibold text-ink">No repositories yet</p>
                    <p className="mt-1 text-[13px] leading-relaxed text-ink-2">
                      Add a public GitHub repository to start asking questions. It counts toward your storage once indexed.
                    </p>
                  </div>
                  <Button to="/app" variant="dark" size="sm" icon={Plus} className="w-full sm:w-auto">
                    Add a repository
                  </Button>
                </div>
              ) : (
                <>
                  <ul className="divide-y divide-line overflow-hidden rounded-2xl border border-line">
                    {shownRepos.map((repo) => {
                      const size = repoSize(repo);
                      const share = Math.min(100, (size / quotaBytes) * 100);
                      return (
                        <li key={repo.id}>
                          <Link
                            to={`/app/r/${encodeURIComponent(repo.repo_name)}`}
                            className="group grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-4 gap-y-1.5 px-4 py-3 transition-colors hover:bg-surface-2 sm:grid-cols-[minmax(0,1fr)_9rem_5rem]"
                          >
                            <span className="min-w-0">
                              <span className="block truncate text-sm font-medium text-ink group-hover:text-accent-strong">{repo.full_name || repo.repo_name}</span>
                              <span className="mt-0.5 flex items-center gap-1.5 text-xs text-ink-2">
                                <Badge tone={statusTone(repo)} dot>{phaseLabel(repo)}</Badge>
                              </span>
                            </span>
                            <span className="order-last col-span-2 sm:order-none sm:col-span-1" aria-hidden="true">
                              {usage && size > 0 && (
                                <span className="block h-1.5 overflow-hidden rounded-full bg-surface-3">
                                  <span className="block h-full rounded-full bg-accent" style={{ width: `${Math.max(2, share)}%` }} />
                                </span>
                              )}
                            </span>
                            <span className="text-right font-mono text-[13px] text-ink">
                              {size > 0 ? formatBytes(size) : <span className="text-ink-3">—</span>}
                              <span className="sr-only">{size > 0 ? ' of indexed source' : ' (size not counted yet)'}</span>
                            </span>
                          </Link>
                        </li>
                      );
                    })}
                  </ul>
                  {repos.length > shownRepos.length && (
                    <p className="mt-3 text-[13px] text-ink-2">
                      And {repos.length - shownRepos.length} more. <Link to="/app" className="font-medium text-accent-strong hover:underline">See all in the workspace</Link>
                    </p>
                  )}
                  <p className="mt-3 text-xs leading-relaxed text-ink-2">
                    To free space, open a repository and choose Delete repository from its ⋯ actions menu.
                  </p>
                </>
              )}
            </div>
          </Card>

          {/* GitHub */}
          <Card labelledBy="github-heading" className="md:col-span-3">
            <CardHeader
              icon={Github}
              id="github-heading"
              title="GitHub"
              description="Used only to open pull requests from changes you have reviewed."
              badge={github.checked && !githubCheckFailed ? (
                <Badge tone={github.connected ? 'success' : 'neutral'} dot>{github.connected ? 'Connected' : 'Not connected'}</Badge>
              ) : null}
            />
            <div className="mt-5">
              {github.checked && githubCheckFailed ? (
                <InlineError onRetry={retryGithubCheck} retrying={githubRetrying}>Your GitHub connection could not be checked right now.</InlineError>
              ) : !github.checked ? (
                <div role="status" aria-label="Checking GitHub connection" className="skeleton h-5 w-64 max-w-full" />
              ) : github.connected ? (
                confirmDisconnect ? (
                  <div className="rounded-2xl border border-line bg-surface-2/70 p-4">
                    <p className="text-sm font-semibold text-ink">Disconnect {github.username ? `@${github.username}` : 'GitHub'}?</p>
                    <p className="mt-1 text-[13px] leading-relaxed text-ink-2">
                      The stored token is deleted. Pull requests you already opened stay on GitHub. You will connect again
                      the next time you open one.
                    </p>
                    <div className="mt-4 flex flex-col gap-2 sm:flex-row">
                      <Button variant="danger" size="sm" icon={Unplug} loading={disconnecting} onClick={handleDisconnect}>
                        Disconnect GitHub
                      </Button>
                      <Button variant="ghost" size="sm" onClick={() => setConfirmDisconnect(false)} disabled={disconnecting}>
                        Keep connected
                      </Button>
                    </div>
                  </div>
                ) : (
                  <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
                    <div className="min-w-0 text-sm text-ink-2">
                      <p>
                        {github.username ? (
                          <>Connected as <span className="font-mono font-medium text-ink">@{github.username}</span></>
                        ) : 'Connected to GitHub'}
                        {github.updatedAt && relativeTime(github.updatedAt) && <span className="text-ink-3"> · since {relativeTime(github.updatedAt)}</span>}
                      </p>
                      <p className="mt-1 text-[13px] leading-relaxed">
                        The token is encrypted at rest and never sent to your browser.
                      </p>
                    </div>
                    <Button
                      variant="danger-ghost"
                      size="sm"
                      icon={Unplug}
                      onClick={() => { setGithubNotice(''); setConfirmDisconnect(true); }}
                      className="w-full border border-line sm:w-auto"
                    >
                      Disconnect
                    </Button>
                  </div>
                )
              ) : (
                <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
                  <p className="min-w-0 text-sm leading-relaxed text-ink-2">
                    {connecting
                      ? 'Finish in the GitHub window. This page updates when it is done.'
                      : 'Connect now, or later from the review screen when you open your first pull request. Without GitHub you can still download any change as a .patch.'}
                  </p>
                  <Button variant="dark" size="sm" icon={Github} loading={connecting} onClick={connectGithub} className="w-full sm:w-auto">
                    {connecting ? 'Waiting for GitHub…' : 'Connect GitHub'}
                  </Button>
                </div>
              )}
              <p role="status" aria-live="polite" className={githubNotice ? 'mt-4 text-sm text-success' : 'sr-only'}>{githubNotice}</p>
              {githubError && <div className="mt-4"><InlineError>{githubError}</InlineError></div>}
              {github.checked && !githubCheckFailed && github.connected && (
                <p className="mt-4 flex flex-wrap items-center gap-1 text-xs text-ink-2">
                  To also revoke the app on GitHub, use
                  <a
                    href="https://github.com/settings/applications"
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-1 font-medium text-ink-2 underline underline-offset-2 hover:text-ink"
                  >
                    GitHub settings → Applications <ExternalLink className="h-3 w-3" aria-hidden="true" />
                    <span className="sr-only">(opens in a new tab)</span>
                  </a>
                </p>
              )}
            </div>
          </Card>
        </div>

        <p className="mt-8 flex items-start gap-2 text-xs leading-relaxed text-ink-2">
          <ShieldCheck className="mt-px h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          Your workspace is scoped to your account. GitHub tokens are encrypted at rest, and nothing is pushed to GitHub without your review.
        </p>
      </div>
    </main>
  );
};

export default AccountPage;
