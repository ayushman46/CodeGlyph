import {
  AlertCircle, Check, CheckCircle2, ClipboardCopy, Columns2, Download, ExternalLink, FileDiff, FilePlus2,
  GitBranch, GitPullRequest, Github, PencilLine, RefreshCw, Rows3, X,
} from 'lucide-react';
import { useCallback, useDeferredValue, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { errorMessage, getEditProposal, getGithubFile, getGithubLoginUrl, pushGithubPR } from '../../api/client';
import { compactPath } from '../../lib/citations';
import { exactStats, restoreLineEndings } from '../../lib/eol';
import { classifyGithubError } from '../../lib/githubErrors';
import { applyHunks, downloadText, proposalPatch, slugify } from '../../lib/patch';
import useFocusTrap from '../../hooks/useFocusTrap';
import useStore from '../../store/useStore';
import Badge from '../ui/Badge';
import Button from '../ui/Button';
import Segmented from '../ui/Segmented';
import Spinner from '../ui/Spinner';
import DiffView, { UI_DIFF_OPTIONS } from './DiffView';
import { approximatePaths, issueLabel } from '../../lib/proposal';
import ProposalNotices from './ProposalNotices';

const randomSuffix = () => Math.random().toString(36).slice(2, 7);
const uid = () => (globalThis.crypto?.randomUUID?.() || `${Date.now()}-${randomSuffix()}`);

const buildBody = (proposal) => {
  const body = String(proposal?.pr_body || '').trim() || `## Summary\n\n${proposal?.summary || 'Reviewed change.'}`;
  const validation = (proposal?.validation || []).filter(Boolean);
  const checklist = validation.length && !/##\s*testing/i.test(body)
    ? `\n\n## Testing\n${validation.map((step) => `- [ ] ${step}`).join('\n')}`
    : '';
  return `${body}${checklist}\n\n---\n_Generated with Codebase Intel and reviewed before pushing._`.slice(0, 5000);
};

const FileListItem = ({ file, active, stats, dirty, approximate, onSelect }) => {
  const created = file.action === 'create';
  const Icon = created ? FilePlus2 : FileDiff;
  const name = file.file_path.split('/').pop();
  const directory = file.file_path.slice(0, -name.length).replace(/\/$/, '');
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-current={active ? 'true' : undefined}
      className={`flex w-full items-start gap-2.5 rounded-xl px-2.5 py-2 text-left transition-colors ${active ? 'bg-accent-soft' : 'hover:bg-surface-2'}`}
    >
      <Icon className={`mt-0.5 h-4 w-4 shrink-0 ${created ? 'text-success' : active ? 'text-accent' : 'text-ink-3'}`} aria-hidden="true" />
      <span className="min-w-0 flex-1" title={file.file_path}>
        <span className={`block truncate font-mono text-[12.5px] ${active ? 'font-semibold text-ink' : 'text-ink-2'}`}>{name}</span>
        {/* Keep the trailing folders, which tell same-named files apart. */}
        {directory && <span className={`block truncate font-mono text-[10.5px] ${active ? 'text-ink-2' : 'text-ink-3'}`}>{compactPath(directory, 30)}</span>}
      </span>
      <span className="shrink-0 text-right font-mono text-[10.5px] leading-4">
        <span className="block text-success">+{stats.additions}</span>
        <span className="block text-danger">−{stats.deletions}</span>
        {dirty && <span className="block text-accent">edited</span>}
        {approximate && <span className="block text-info" title="Reconstructed from the index; refresh from GitHub before pushing.">approx.</span>}
      </span>
    </button>
  );
};

const initialContent = (files) => Object.fromEntries(files.map((file) => [file.file_path, file.after ?? null]));

// Stats are cached per file and recomputed only for the file whose content
// changed, so typing in one file never re-diffs every other file.
const useFileStats = (files, contentByPath) => {
  const cache = useRef(new Map());
  return useMemo(() => Object.fromEntries(files.map((file) => {
    const after = contentByPath[file.file_path];
    if (after == null) return [file.file_path, { additions: file.additions ?? 0, deletions: file.deletions ?? 0 }];
    const before = file.action === 'create' ? '' : (file.before ?? '');
    const cached = cache.current.get(file.file_path);
    if (cached && cached.before === before && cached.after === after) return [file.file_path, cached.stats];
    const stats = exactStats(before, after, UI_DIFF_OPTIONS);
    cache.current.set(file.file_path, { before, after, stats });
    return [file.file_path, stats];
  })), [files, contentByPath]);
};

/**
 * Full-screen review: every proposed file as a diff (unified/split) or
 * editable text, a prefilled PR form, GitHub connection, and push.
 *
 * Mounted only while a review is open and keyed by the proposal, so no state
 * (files, in-flight refreshes, edits) can leak from one proposal into the next.
 */
const PullRequestModal = () => {
  const review = useStore((state) => state.review);
  if (!review) return null;
  return <PullRequestReview key={`${review.messageId}:${review.proposal?.id ?? ''}`} review={review} />;
};

const PullRequestReview = ({ review }) => {
  const closeReview = useStore((state) => state.closeReview);
  const github = useStore((state) => state.github);
  const checkGithub = useStore((state) => state.checkGithub);
  const setGithub = useStore((state) => state.setGithub);
  const proposal = review?.proposal;
  const repoName = review?.repoName;

  const [files, setFiles] = useState(() => (proposal?.files || []).map((file) => ({ ...file })));
  const [contentByPath, setContentByPath] = useState(() => initialContent(proposal?.files || []));
  const [selectedPath, setSelectedPath] = useState(() => proposal?.files?.[0]?.file_path || '');
  const [view, setView] = useState('unified');
  const [editing, setEditing] = useState(false);
  const [ticket, setTicket] = useState(() => review?.ticket || '');
  const [title, setTitle] = useState(() => (proposal?.title || 'Codebase Intel change').slice(0, 300));
  const [body, setBody] = useState(() => (proposal ? buildBody(proposal) : ''));
  const [branch, setBranch] = useState(() => `codebase-intel/${slugify(proposal?.title, 'change').slice(0, 40)}-${randomSuffix()}`);
  const [commitMessage, setCommitMessage] = useState(() => (proposal?.title || 'Apply reviewed changes').slice(0, 500));
  const [loadingFiles, setLoadingFiles] = useState(false);
  const [fileErrors, setFileErrors] = useState({});
  const [error, setError] = useState('');
  // What the error lets the user do next: 'new_branch' | 'refresh' | null.
  const [errorAction, setErrorAction] = useState(null);
  // Rate limited: the earliest time a retry makes sense, with a countdown.
  const [retryAt, setRetryAt] = useState(0);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!retryAt) return undefined;
    const timer = window.setInterval(() => {
      setNow(Date.now());
      if (Date.now() >= retryAt) setRetryAt(0);
    }, 1000);
    return () => window.clearInterval(timer);
  }, [retryAt]);
  const waitSeconds = retryAt ? Math.max(0, Math.ceil((retryAt - now) / 1000)) : 0;
  const [connecting, setConnecting] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [result, setResult] = useState(null);
  const [copied, setCopied] = useState(false);
  const idempotencyKey = useRef(uid());
  const popupRef = useRef(null);
  const pollRef = useRef(null);
  const closeRef = useRef(null);
  const dialogRef = useRef(null);
  const alertRef = useRef(null);
  const resultHeadingRef = useRef(null);
  // Results of requests started by this review only; cleared on unmount so a
  // slow refresh can never write into a later review.
  const aliveRef = useRef(true);

  const busy = submitting || connecting;
  const approximate = useMemo(() => approximatePaths(proposal), [proposal]);
  const refreshedPaths = useMemo(() => new Set(files.filter((file) => file.refreshed).map((file) => file.file_path)), [files]);

  useEffect(() => {
    aliveRef.current = true;
    if (proposal) void checkGithub();
    return () => { aliveRef.current = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const freshTicket = useCallback(async ({ force = false } = {}) => {
    if (ticket && !force) return ticket;
    const response = await getEditProposal(proposal.id);
    setTicket(response.data.edit_ticket || '');
    return response.data.edit_ticket || '';
  }, [proposal?.id, ticket]);

  /** Load files whose content or blob SHA is missing (large or index-sourced). */
  const refreshFromGithub = useCallback(async (paths, sourceFiles = files) => {
    if (!proposal) return;
    const targets = sourceFiles.filter((file) => file.action !== 'create' && (paths ? paths.includes(file.file_path) : true));
    if (!targets.length) return;
    setLoadingFiles(true);
    setError('');
    try {
      let activeTicket = await freshTicket();
      const errors = {};
      const updates = {};
      for (const file of targets) {
        try {
          let response;
          try {
            response = await getGithubFile(repoName, file.file_path, activeTicket);
          } catch (requestError) {
            // Only an expired edit ticket is retried; a GitHub permission
            // refusal would fail the same way with any ticket.
            if (!classifyGithubError(requestError).retryWithFreshTicket) throw requestError;
            activeTicket = await freshTicket({ force: true });
            response = await getGithubFile(repoName, file.file_path, activeTicket);
          }
          if (!aliveRef.current) return;
          const current = response.data;
          // Index-reconstructed files are always re-applied to the real blob.
          const unchanged = !approximate.has(file.file_path) && file.base_blob_sha && current.sha === file.base_blob_sha && file.after != null;
          if (unchanged) {
            updates[file.file_path] = { before: file.before, after: file.after, base_blob_sha: current.sha };
          } else {
            updates[file.file_path] = { before: current.content, after: applyHunks(current.content, file.changes), base_blob_sha: current.sha, refreshed: true };
          }
        } catch (fileError) {
          if (fileError?.response?.status === 401) setGithub({ connected: false, username: '' });
          errors[file.file_path] = fileError?.response ? classifyGithubError(fileError).message : fileError.message;
        }
      }
      if (!aliveRef.current) return;
      setFiles((current) => current.map((file) => (updates[file.file_path] ? { ...file, ...updates[file.file_path] } : file)));
      setContentByPath((current) => {
        const next = { ...current };
        Object.entries(updates).forEach(([path, update]) => {
          // Keep the user's manual edits unless the base revision changed.
          if (update.refreshed || next[path] == null) next[path] = update.after;
        });
        return next;
      });
      setFileErrors(errors);
    } catch (loadError) {
      if (aliveRef.current) setError(errorMessage(loadError, 'Could not refresh the files from GitHub.'));
    } finally {
      if (aliveRef.current) setLoadingFiles(false);
    }
  }, [files, freshTicket, proposal, repoName]);

  // Files of THIS proposal that arrived without content or a revision are
  // loaded once on open (derived from the proposal, never from stale state).
  useEffect(() => {
    const proposalFiles = (proposal?.files || []).map((file) => ({ ...file }));
    // Index-reconstructed (approximate) files are re-applied to the real blob too.
    const missing = proposalFiles.filter((file) => file.action !== 'create' && (file.after == null || !file.base_blob_sha || approximate.has(file.file_path))).map((file) => file.file_path);
    if (missing.length) void refreshFromGithub(missing, proposalFiles);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Keyboard: focus stays inside, Escape closes (unless busy), focus returns
  // to the button that opened the review; the page behind does not scroll.
  const busyRef = useRef(busy);
  busyRef.current = busy;
  useFocusTrap(dialogRef, true, {
    initialFocusRef: closeRef,
    onEscape: () => { if (!busyRef.current) closeReview(); },
  });
  useEffect(() => {
    const { overflow } = document.body.style;
    document.body.style.overflow = 'hidden';
    return () => { document.body.style.overflow = overflow; };
  }, []);

  // A push or load error is shown next to the button that caused it, even
  // when the form scrolls on small screens.
  useEffect(() => {
    if (error) alertRef.current?.scrollIntoView?.({ block: 'nearest' });
  }, [error]);

  // Announce the created pull request by moving focus to its heading.
  useEffect(() => {
    if (result) window.setTimeout(() => resultHeadingRef.current?.focus(), 0);
  }, [result]);

  // OAuth popup result: postMessage when the opener survives, polling always.
  useEffect(() => {
    const onMessage = (event) => {
      if (event.source !== popupRef.current) return;
      if (event.data?.type === 'GITHUB_AUTH_ERROR') {
        setConnecting(false);
        setError(event.data.error || 'GitHub authorization failed.');
      } else if (event.data?.type === 'GITHUB_AUTH_SUCCESS') {
        void checkGithub().then(() => setConnecting(false));
      }
    };
    window.addEventListener('message', onMessage);
    return () => {
      window.removeEventListener('message', onMessage);
      if (pollRef.current) window.clearInterval(pollRef.current);
    };
  }, [checkGithub]);

  const connectGithub = async () => {
    setConnecting(true);
    setError('');
    try {
      const response = await getGithubLoginUrl('/app');
      const url = response.data?.authorization_url;
      if (!url) throw new Error('GitHub authorization is unavailable.');
      const width = 600;
      const height = 720;
      const left = window.screenX + Math.max(0, (window.outerWidth - width) / 2);
      const top = window.screenY + Math.max(0, (window.outerHeight - height) / 2);
      const popup = window.open(url, 'codebase-intel-github', `width=${width},height=${height},left=${left},top=${top}`);
      if (!popup) throw new Error('Allow pop-ups for this site to connect GitHub.');
      popupRef.current = popup;
      const startedConnection = github.updatedAt;
      const deadline = Date.now() + 5 * 60 * 1000;
      if (pollRef.current) window.clearInterval(pollRef.current);
      pollRef.current = window.setInterval(async () => {
        const status = await checkGithub();
        const connectedNow = status?.connected && status.updatedAt !== startedConnection;
        if (connectedNow || Date.now() > deadline || (popup.closed && !connectedNow)) {
          window.clearInterval(pollRef.current);
          pollRef.current = null;
          setConnecting(false);
          if (connectedNow) setGithub(status);
          try { if (connectedNow && !popup.closed) popup.close(); } catch { /* cross-origin */ }
        }
      }, 2000);
    } catch (connectError) {
      setConnecting(false);
      setError(errorMessage(connectError, connectError.message || 'Could not start GitHub authorization.'));
    }
  };

  // Deferred so typing in the editor stays responsive; stats catch up.
  const deferredContent = useDeferredValue(contentByPath);
  const statsByPath = useFileStats(files, deferredContent);

  const totals = Object.values(statsByPath).reduce((sum, item) => ({ additions: sum.additions + item.additions, deletions: sum.deletions + item.deletions }), { additions: 0, deletions: 0 });
  const selected = files.find((file) => file.file_path === selectedPath) || files[0];
  const selectedContent = selected ? contentByPath[selected.file_path] : null;
  const dirtyPaths = new Set(files.filter((file) => file.after != null && contentByPath[file.file_path] != null && contentByPath[file.file_path] !== file.after).map((file) => file.file_path));

  const patchText = () => proposalPatch({ files }, contentByPath);

  const copyPatch = async () => {
    try {
      await navigator.clipboard.writeText(patchText());
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setError('Clipboard access was blocked. Use Download instead.');
    }
  };

  const useNewBranch = () => {
    setBranch(`codebase-intel/${slugify(title || proposal?.title, 'change').slice(0, 40)}-${randomSuffix()}`);
    idempotencyKey.current = uid();
    setError('');
    setErrorAction(null);
  };

  const push = async () => {
    setError('');
    setErrorAction(null);
    const problems = [];
    if (!/^codebase-intel\/[A-Za-z0-9._\-/]+$/.test(branch) || branch.includes('..') || branch.endsWith('/') || branch.endsWith('.')) {
      problems.push('Branch names must start with codebase-intel/ and use letters, numbers, dots, dashes or slashes.');
    }
    if (!title.trim()) problems.push('Add a pull request title.');
    files.forEach((file) => {
      const content = contentByPath[file.file_path];
      // The real reason (e.g. a hunk that no longer matches) comes first.
      if (fileErrors[file.file_path]) problems.push(`${file.file_path}: ${fileErrors[file.file_path]}`);
      else if (content == null || content === '') problems.push(`${file.file_path} has no content.`);
      else if (file.action !== 'create' && !file.base_blob_sha) problems.push(`${file.file_path} has not been loaded from GitHub yet.`);
      else if (approximate.has(file.file_path) && !file.refreshed) problems.push(`${file.file_path} was reconstructed from the index. Use "Refresh from GitHub" before pushing.`);
    });
    if (problems.length) {
      setError(problems[0]);
      return;
    }
    setSubmitting(true);
    try {
      const payload = (activeTicket) => ({
        repo_name: repoName,
        files: files.map((file) => ({
          file_path: file.file_path,
          new_content: contentByPath[file.file_path],
          file_sha: file.action === 'create' ? '' : file.base_blob_sha,
          action: file.action === 'create' ? 'create' : 'modify',
        })),
        branch_name: branch.trim(),
        commit_message: commitMessage.trim() || title.trim(),
        pr_title: title.trim(),
        pr_body: body.trim(),
        idempotency_key: idempotencyKey.current,
        edit_ticket: activeTicket,
      });
      let response;
      try {
        response = await pushGithubPR(payload(await freshTicket()));
      } catch (pushError) {
        if (!classifyGithubError(pushError).retryWithFreshTicket) throw pushError;
        response = await pushGithubPR(payload(await freshTicket({ force: true })));
      }
      setResult(response.data);
    } catch (pushError) {
      const failure = pushError?.response ? classifyGithubError(pushError) : null;
      if (!failure) {
        setError(errorMessage(pushError, 'GitHub could not create the pull request.'));
      } else {
        // 401 is the only status meaning the token itself is gone.
        if (failure.disconnect) setGithub({ connected: false, username: '' });
        // Same-key retries resume server-side (400/429/503/...); only a
        // conflict that needs a different push starts a new operation.
        if (!failure.keepKey) idempotencyKey.current = uid();
        if (failure.kind === 'rate_limited') {
          const at = Date.now() + (failure.retryAfter ?? 30) * 1000;
          setNow(Date.now());
          setRetryAt(at);
        }
        setError(failure.message);
        setErrorAction(failure.action === 'new_branch' || failure.action === 'refresh' ? failure.action : null);
      }
    } finally {
      setSubmitting(false);
    }
  };

  const content = (
    <div ref={dialogRef} tabIndex={-1} className="fixed inset-0 z-[85] flex flex-col bg-canvas animate-fade-in outline-none" role="dialog" aria-modal="true" aria-labelledby="pr-review-title">
      <div className="flex h-14 shrink-0 items-center justify-between gap-3 border-b border-line bg-surface px-3 sm:px-5">
        <div className="flex min-w-0 items-center gap-3">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-accent-soft text-accent"><GitPullRequest className="h-4 w-4" /></div>
          <div className="min-w-0">
            <h2 id="pr-review-title" className="truncate text-sm font-semibold text-ink">{result ? 'Pull request ready' : 'Review changes'}</h2>
            <p className="truncate text-[11.5px] text-ink-3">
              {repoName} · {files.length} file{files.length === 1 ? '' : 's'} ·{' '}
              <span className="text-success">+{totals.additions}</span> <span className="text-danger">−{totals.deletions}</span>
            </p>
          </div>
        </div>
        <button ref={closeRef} type="button" onClick={closeReview} disabled={busy} className="rounded-lg p-2 text-ink-3 hover:bg-surface-2 hover:text-ink disabled:opacity-40" aria-label="Close review">
          <X className="h-4 w-4" />
        </button>
      </div>

      {review.loading ? (
        <div className="flex flex-1 items-center justify-center text-ink-3"><Spinner className="h-5 w-5" label="Loading proposal" /></div>
      ) : review.error && !proposal ? (
        <div className="flex flex-1 items-center justify-center p-6">
          <div className="max-w-md rounded-2xl border border-danger/30 bg-danger-soft p-5 text-sm text-danger">{review.error}</div>
        </div>
      ) : result ? (
        <div className="flex flex-1 items-center justify-center p-6">
          <div className="w-full max-w-lg animate-rise rounded-3xl border border-line bg-surface p-8 text-center shadow-[var(--shadow-lift)]">
            <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-full bg-success-soft text-success"><CheckCircle2 className="h-7 w-7" /></div>
            <h3 ref={resultHeadingRef} tabIndex={-1} className="mt-5 text-xl font-semibold tracking-tight text-ink outline-none">
              Pull request #{result.pr_number} {result.already_existed ? 'already open' : 'created'}
            </h3>
            <p className="mt-2 text-sm leading-relaxed text-ink-3">
              Committed to <span className="font-mono text-ink-2">{result.branch_name}</span>
              {result.is_fork ? <> in your fork <span className="font-mono text-ink-2">{result.target_repo}</span></> : null}.
            </p>
            <div className="mt-7 flex flex-wrap justify-center gap-2.5">
              <Button variant="primary" size="lg" href={result.pr_url} target="_blank" rel="noreferrer" iconRight={ExternalLink}>View on GitHub</Button>
              <Button variant="secondary" size="lg" onClick={closeReview}>Done</Button>
            </div>
          </div>
        </div>
      ) : (
        <div className="grid min-h-0 flex-1 grid-cols-1 overflow-y-auto lg:grid-cols-[17rem_minmax(0,1fr)] lg:grid-rows-[minmax(0,1fr)_auto] lg:overflow-hidden xl:grid-cols-[17rem_minmax(0,1fr)_22rem] xl:grid-rows-1">
          {/* Files */}
          <nav className="hidden min-h-0 flex-col border-r border-line bg-surface lg:row-span-2 lg:flex xl:row-span-1" aria-label="Changed files">
            <div className="flex items-center justify-between px-4 pb-2 pt-4">
              <p className="text-[11px] font-semibold uppercase tracking-wider text-ink-3">Files changed</p>
              <Badge tone="neutral">{files.length}</Badge>
            </div>
            <div className="scroll-thin min-h-0 flex-1 space-y-0.5 overflow-y-auto px-2 pb-3">
              {files.map((file) => (
                <FileListItem key={file.file_path} file={file} approximate={approximate.has(file.file_path) && !file.refreshed} active={file.file_path === selected?.file_path} stats={statsByPath[file.file_path] || { additions: 0, deletions: 0 }} dirty={dirtyPaths.has(file.file_path)} onSelect={() => setSelectedPath(file.file_path)} />
              ))}
            </div>
            <div className="space-y-1.5 border-t border-line p-3">
              <Button size="sm" variant="ghost" icon={RefreshCw} loading={loadingFiles} className="w-full justify-start" onClick={() => refreshFromGithub()}>Refresh from GitHub</Button>
              <Button size="sm" variant="ghost" icon={Download} className="w-full justify-start" onClick={() => downloadText(`${slugify(repoName)}-${slugify(title)}.patch`, patchText())}>Download .patch</Button>
              <Button size="sm" variant="ghost" icon={copied ? Check : ClipboardCopy} className="w-full justify-start" onClick={copyPatch}>{copied ? 'Copied' : 'Copy patch'}</Button>
            </div>
          </nav>

          {/* Diff / editor */}
          <div className="flex min-h-[60vh] flex-col lg:min-h-0">
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line bg-surface px-3 py-2 sm:px-4">
              <div className="flex min-w-0 items-center gap-2">
                <select
                  className="max-w-[60vw] truncate rounded-lg border border-line bg-surface px-2 py-1.5 font-mono text-[12px] text-ink lg:hidden"
                  value={selected?.file_path || ''}
                  onChange={(event) => setSelectedPath(event.target.value)}
                  aria-label="Select file"
                >
                  {files.map((file) => <option key={file.file_path} value={file.file_path}>{file.file_path}</option>)}
                </select>
                <span className="hidden truncate font-mono text-[12.5px] font-medium text-ink lg:block">{selected?.file_path}</span>
                {selected?.action === 'create' && <Badge tone="success">New file</Badge>}
                {selected?.refreshed && <Badge tone="info">Re-applied to latest</Badge>}
              </div>
              <div className="flex items-center gap-2">
                {!editing && (
                  <Segmented
                    size="xs"
                    label="Diff layout"
                    value={view}
                    onChange={setView}
                    options={[{ value: 'unified', label: 'Unified', icon: Rows3 }, { value: 'split', label: 'Split', icon: Columns2 }]}
                  />
                )}
                <Button size="xs" variant={editing ? 'dark' : 'secondary'} icon={PencilLine} onClick={() => setEditing((value) => !value)} disabled={selectedContent == null}>
                  {editing ? 'Show diff' : 'Edit'}
                </Button>
              </div>
            </div>
            <div className="scroll-thin min-h-0 flex-1 overflow-auto p-3 sm:p-4">
              <ProposalNotices proposal={proposal} refreshedPaths={refreshedPaths} className="mb-3" />
              {proposal?.issue_reference && <p className="mb-2 text-[12px] text-ink-3">{issueLabel(proposal)}</p>}
              {selected?.summary && <p className="mb-3 text-[13px] leading-relaxed text-ink-2">{selected.summary}</p>}
              {fileErrors[selected?.file_path] && (
                <div className="mb-3 flex items-start gap-2 rounded-xl border border-danger/30 bg-danger-soft p-3 text-[13px] text-danger">
                  <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                  <span>{fileErrors[selected.file_path]} Regenerate the change from the chat if the file changed upstream.</span>
                </div>
              )}
              {selectedContent == null ? (
                <div className="flex items-center gap-2 rounded-xl border border-line bg-surface p-6 text-sm text-ink-3">
                  {loadingFiles ? <><Spinner /> Loading the current file from GitHub…</> : 'This file could not be loaded.'}
                </div>
              ) : editing ? (
                <textarea
                  // Give the textarea the LF view it would report anyway, so
                  // React never rewrites the value (and the caret) per key.
                  value={selectedContent.replace(/\r\n/g, '\n')}
                  // A textarea always reports LF; put the file's own line
                  // endings back so a CRLF file is not rewritten wholesale.
                  onChange={(event) => {
                    const { value } = event.target;
                    setContentByPath((current) => ({ ...current, [selected.file_path]: restoreLineEndings(value, current[selected.file_path] ?? selected.after ?? '') }));
                  }}
                  spellCheck={false}
                  aria-label={`Edit ${selected.file_path}`}
                  className="code-scroll h-[min(70vh,44rem)] w-full resize-none rounded-xl border border-code-line bg-code p-4 font-mono text-[12.5px] leading-relaxed text-code-ink outline-none focus:border-accent"
                />
              ) : (
                <DiffView key={`${selected.file_path}-${view}`} before={selected.action === 'create' ? '' : selected.before} after={selectedContent} view={view} />
              )}
              {dirtyPaths.has(selected?.file_path) && !editing && (
                <button type="button" onClick={() => setContentByPath((current) => ({ ...current, [selected.file_path]: selected.after }))} className="mt-2 text-[12px] font-medium text-accent hover:underline">
                  Discard my edits to this file
                </button>
              )}
            </div>
          </div>

          {/* Pull request form */}
          {/* Below lg the whole dialog body scrolls as one column; the form
              only gets its own scroller next to the diff. */}
          <aside className="flex flex-col border-t border-line bg-surface lg:col-start-2 lg:max-h-[46vh] lg:min-h-0 xl:col-start-3 xl:max-h-none xl:border-l xl:border-t-0" aria-label="Pull request details">
            <div className="scroll-thin space-y-4 p-4 lg:min-h-0 lg:flex-1 lg:overflow-y-auto">
              <div className="rounded-xl border border-line bg-surface-2/60 p-3">
                <div className="flex items-center gap-2.5">
                  <Github className="h-4 w-4 shrink-0 text-ink" aria-hidden="true" />
                  {!github.checked ? (
                    <span className="flex items-center gap-2 text-[13px] text-ink-3"><Spinner className="h-3.5 w-3.5" /> Checking GitHub…</span>
                  ) : github.connected ? (
                    <span className="text-[13px] text-ink-2">Connected as <span className="font-semibold text-ink">@{github.username}</span></span>
                  ) : (
                    <span className="text-[13px] text-ink-2">Connect GitHub to open the pull request.</span>
                  )}
                </div>
                {github.checked && !github.connected && (
                  <Button size="sm" variant="dark" icon={Github} loading={connecting} className="mt-3 w-full" onClick={connectGithub}>
                    {connecting ? 'Waiting for GitHub…' : 'Connect GitHub'}
                  </Button>
                )}
                <p className="mt-2 text-[11.5px] leading-relaxed text-ink-3">
                  With write access the branch is created in the repository; otherwise in your fork, with a PR to the original.
                </p>
              </div>

              <label className="block">
                <span className="mb-1 block text-[12px] font-medium text-ink-2">Title</span>
                <input value={title} maxLength={300} onChange={(event) => setTitle(event.target.value)} className="h-9 w-full rounded-lg border border-line bg-surface px-3 text-[13px] text-ink outline-none focus:border-accent" />
              </label>
              <label className="block">
                <span className="mb-1 block text-[12px] font-medium text-ink-2">Description</span>
                <textarea value={body} maxLength={5000} onChange={(event) => setBody(event.target.value)} rows={9} className="scroll-thin w-full resize-y rounded-lg border border-line bg-surface px-3 py-2 font-mono text-[12px] leading-relaxed text-ink outline-none focus:border-accent" />
              </label>
              <label className="block">
                <span className="mb-1 flex items-center gap-1.5 text-[12px] font-medium text-ink-2"><GitBranch className="h-3.5 w-3.5" /> Branch</span>
                <input value={branch} maxLength={100} onChange={(event) => setBranch(event.target.value)} className="h-9 w-full rounded-lg border border-line bg-surface px-3 font-mono text-[12px] text-ink outline-none focus:border-accent" />
              </label>
              <label className="block">
                <span className="mb-1 block text-[12px] font-medium text-ink-2">Commit message</span>
                <input value={commitMessage} maxLength={500} onChange={(event) => setCommitMessage(event.target.value)} className="h-9 w-full rounded-lg border border-line bg-surface px-3 text-[13px] text-ink outline-none focus:border-accent" />
              </label>
              {error && (
                <div ref={alertRef} role="alert" className="flex items-start gap-2 rounded-xl border border-danger/30 bg-danger-soft p-3 text-[12.5px] leading-relaxed text-danger">
                  <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                  <div className="min-w-0">
                    <span>{error}</span>
                    {errorAction === 'new_branch' && (
                      <button type="button" onClick={useNewBranch} className="mt-1.5 block font-semibold underline">Use a new branch name</button>
                    )}
                    {errorAction === 'refresh' && (
                      <button type="button" onClick={() => refreshFromGithub()} className="mt-1.5 block font-semibold underline">Refresh from GitHub</button>
                    )}
                  </div>
                </div>
              )}
            </div>
            <div className="space-y-2 border-t border-line p-4">
              <Button
                variant="primary"
                size="lg"
                icon={GitPullRequest}
                className="w-full"
                loading={submitting}
                disabled={!github.connected || loadingFiles || !files.length || waitSeconds > 0}
                onClick={push}
              >
                {submitting ? 'Creating pull request…' : waitSeconds > 0 ? `Try again in ${waitSeconds}s` : 'Create pull request'}
              </Button>
              <div className="flex gap-2 lg:hidden">
                <Button size="sm" variant="secondary" icon={Download} className="flex-1" onClick={() => downloadText(`${slugify(repoName)}-${slugify(title)}.patch`, patchText())}>.patch</Button>
                <Button size="sm" variant="secondary" icon={RefreshCw} className="flex-1" loading={loadingFiles} onClick={() => refreshFromGithub()}>Refresh</Button>
              </div>
              {github.checked && !github.connected && (
                <p className="text-center text-[11.5px] text-ink-2">Connect GitHub above to create the pull request.</p>
              )}
              <p className="text-center text-[11px] text-ink-3">Nothing is pushed until you click Create pull request.</p>
            </div>
          </aside>
        </div>
      )}
    </div>
  );

  return createPortal(content, document.body);
};

export default PullRequestModal;
